# HASHI Remote Fix Bundle — 2026-05-26

**Status:** Implemented on `hashi_remote_fix`  
**Date:** 2026-05-26  
**Scope:** Remote routing, peer visibility, split-home runtime state, operator callback safety, and HASHI2 watchdog validation

## 1. Summary

This document records the full fix bundle currently staged on
`hashi_remote_fix`.

The bundle contains three related groups of work:

1. Remote routing and peer-state fixes
2. `/jobs` and `/nudge` callback tokenization fixes
3. HASHI2 remote watchdog and stability-window fixes

These changes were developed together because they were discovered during the
same real remote debugging cycle, even though they touch different layers.

## 2. Remote Fixes

### 2.1 `/remote` output is simpler and more truthful

Files:

- `orchestrator/runtime_remote.py`
- `tests/test_runtime_remote.py`

What changed:

- `/remote` now shows the status summary and the current remote instance list
  together
- `/remote list` remains available, but it is now treated as a legacy view
- `/remote list` no longer forces a peer refresh during display
- peer rendering was consolidated into one shared helper

Why:

- viewing remote state should not trigger a new synchronous liveness check
- the main operator path should be `/remote`, not `/remote` plus `/remote list`
- the display path should read the local peer view that the remote system
  already maintains

### 2.2 LAN discovery no longer trusts loopback too early

Files:

- `remote/peer/lan.py`
- `tests/test_remote_peer_status.py`

What changed:

- peer host selection now prefers advertised non-loopback candidates over mDNS
  loopback addresses

Why:

- some peers advertised valid LAN addresses while discovery still surfaced a
  loopback endpoint first
- this led to false same-host style routing on peers that were actually
  cross-instance LAN targets

### 2.3 Canonical peer rebuild no longer carries stale route failure state

Files:

- `remote/peer/registry.py`
- `tests/test_remote_peer_status.py`

What changed:

- previous handshake and liveness metadata is only preserved when the canonical
  route did not change
- if a peer moves from a stale fallback route to a live discovered route, the
  old timeout/offline state is dropped

Why:

- otherwise the registry can keep reporting a peer as failed even after the
  route has been corrected

### 2.4 Handshake payload host choice is now route-aware

Files:

- `remote/protocol_manager.py`
- `tests/test_remote_peer_status.py`

What changed:

- inbound handshake reverse-registration now prefers the best advertised LAN or
  routable host instead of blindly trusting loopback `client_ip`
- bootstrap fallback will no longer re-inject stale routes when live discovery
  already owns the peer

Why:

- this prevents healthy discovered peers from being polluted by fallback data
- it also keeps Windows/WSL mixed instances from collapsing back to loopback

### 2.5 Same-host loopback is now fallback, not first choice

Files:

- `remote/routing.py`
- `tests/test_remote_routing.py`

What changed:

- same-host loopback candidates are deferred until canonical/LAN candidates have
  been considered

Why:

- this keeps route selection aligned with the live peer model
- loopback remains available as a rescue route without suppressing better live
  addresses

### 2.6 `hchat_send` now chooses hosts in a safer order

Files:

- `tools/hchat_send.py`
- `tests/test_hchat_send.py`

What changed:

- host candidate ordering now prefers non-loopback canonical, LAN, and overlay
  addresses before loopback
- exchange routing and workbench routing follow the same preference order

Why:

- probe delivery failures were often caused by reaching for loopback too early
- the send path now matches the fixed route model

## 3. Split-Home Runtime State Fixes

Files:

- `orchestrator/flexible_agent_runtime.py`
- `tests/test_remote_peer_status.py`

What changed:

- remote runtime state can now resolve from `bridge_home` when code root and
  runtime home are separated
- `agents.json`, `instances.json`, and runtime claim reads now come from the
  runtime state root, while `remote/config.yaml` still comes from the code root

Why:

- HASHI9-style layouts separate code from runtime state
- remote lifecycle and peer rendering need to read the correct live state when
  those roots are split

## 4. `/jobs` And `/nudge` Callback Fixes

Files:

- `orchestrator/runtime_jobs.py`
- `orchestrator/runtime_nudge.py`
- `orchestrator/flexible_agent_runtime.py`
- `orchestrator/legacy/bridge_agent_runtime.py`
- `tests/test_agent_runtime_job_transfer.py`
- `tests/test_nudge_command.py`
- `docs/JOBS_CALLBACK_TOKENIZATION_FIX_PLAN.md`

What changed:

- `/jobs` and `/nudge` no longer embed full job ids in Telegram callback data
- action callbacks are now tokenized into short-lived runtime keys
- expired or invalid callback keys now produce explicit operator feedback

Why:

- Telegram limits `callback_data` to 64 bytes
- the watchdog heartbeat id was long enough to break `/jobs`
- the same raw-id pattern also existed in `/nudge`

Impact:

- long heartbeat ids and long nudge ids are now safe
- operator control panels remain usable without renaming jobs

## 5. HASHI2 Watchdog Fixes

Files:

- `scripts/hashi_remote_watchdog.py`
- `tests/test_hashi_remote_watchdog.py`

What changed:

- added a dedicated HASHI2 watchdog script that:
  - checks remote health
  - probes selected peers
  - records a rolling 7-day stability window
  - can restart HASHI2 remote locally if it is actually down
- fixed a bookkeeping bug where the same unresolved failure could incorrectly
  reset the stability window on every repeated run

Why:

- the 7-day observation loop needs evidence, not informal status updates
- repeated observations of the same unresolved bug should not be counted as new
  bugs

## 6. Validation

The following validations passed on this branch:

- `pytest -q tests/test_runtime_remote.py tests/test_remote_peer_status.py tests/test_remote_routing.py tests/test_hchat_send.py tests/test_hashi_remote_watchdog.py`
- `pytest -q tests/test_agent_runtime_job_transfer.py tests/test_nudge_command.py`
- `python3 -m py_compile orchestrator/runtime_jobs.py orchestrator/runtime_nudge.py orchestrator/flexible_agent_runtime.py orchestrator/legacy/bridge_agent_runtime.py scripts/hashi_remote_watchdog.py`

Live validation also confirmed:

- `/remote list` no longer falsely refreshes and no longer reports an empty peer
  set while peers are visible
- `HASHI2 -> HASHI1` watchdog probes recovered
- `HASHI2 -> HASHI9` watchdog probes recovered

## 7. Notes

- This bundle intentionally keeps the fixes together because they were produced
  during one remote remediation pass.
- The watchdog script is operationally specific to HASHI2, but it is included in
  the bundle because it was required to verify the remote fixes and to catch a
  real stability-window bug.
