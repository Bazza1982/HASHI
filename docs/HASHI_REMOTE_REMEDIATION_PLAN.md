# HASHI Remote Remediation Plan

**Status:** Active remediation plan  
**Date:** 2026-04-27  
**Scope:** HASHI Remote peer visibility, same-host routing, startup verification, operator diagnostics, and validation discipline

## 1. Purpose

This document records:

1. The real problems currently affecting `HASHI Remote`
2. The mistakes made during previous diagnosis and verification
3. The rules that must be followed to avoid wasting developer time
4. A phased implementation plan to fix the remaining issues
5. The exact validation gates required before claiming the work is done

This document exists because previous status reports were too optimistic, too early, and not grounded in sufficient live verification.

## 2. What Went Wrong

The system is not failing for one single reason. There are multiple overlapping problems:

1. Same-host detection is incomplete
2. Preferred-backend selection does not handle all same-machine topologies
3. Routing and UI display are only partially aligned
4. Peer status wording is inconsistent with actual reachability
5. Startup verification was improved, but not enough
6. Operational scripts can misreport reality
7. Earlier developer updates claimed success before end-to-end verification was complete

## 3. Mistakes Made Previously

These are process failures and must be treated as concrete engineering mistakes.

### 3.1 Claimed success before remote instances were actually revalidated

What happened:

- A code change was made
- A local unit test passed
- A commit was created
- Success was reported before all relevant live instances had pulled, restarted, and been rechecked

Why this was wrong:

- Passing a local unit test does not prove network behavior is correct
- A local repo state does not prove another instance is running the new code
- A commit existing locally does not prove `origin/main` was updated

Required rule:

- Never report a remote fix as complete until:
  - local `HEAD` is verified
  - `origin/main` is verified
  - each relevant remote instance `HEAD` is verified
  - each relevant runtime has been restarted or proven to be running the updated code
  - live endpoints have been rechecked after restart

### 3.2 Reported “pushed to main” before verifying `origin/main`

What happened:

- A local commit existed
- A push step appeared to have been done
- The branch was still actually ahead of `origin/main`

Why this was wrong:

- It caused downstream instances to pull old code
- It created false confidence
- It wasted time on invalid downstream testing

Required rule:

- After every push, immediately verify:
  - `git rev-parse HEAD`
  - `git rev-parse origin/main`
- These two values must match before claiming anything was pushed

### 3.3 Interpreted stale or incomplete runtime state as proof of a code bug

What happened:

- A remote instance showed old behavior
- The diagnosis jumped too quickly to “wrong code still running” or “display logic still broken”
- In some cases the process state had changed, but the reasoning had not been refreshed against current live facts

Why this was wrong:

- The code path, the running process, the repo checkout, and the peer data can all drift independently
- Old observations can become false within minutes

Required rule:

- Any statement about a live remote instance must be based on current checks performed in the same debugging pass
- Do not rely on stale observations once a restart, pull, or topology change has occurred

### 3.4 Failed to separate “wrong instance tested” from “wrong behavior observed”

What happened:

- The operator reported that `HASHI1` had pulled and turned on remote
- The Telegram transcript actually showed `/remote on` executed on `HASHI2`
- The distinction was not made quickly enough

Why this was wrong:

- It mixed operator context with system behavior
- It made the discussion more confusing than necessary

Required rule:

- Always identify the responding instance from the actual response payload before drawing conclusions
- If the operator says “instance X” but the response clearly came from instance Y, state that immediately and explicitly

### 3.5 Fixed UI display partially, but not the underlying same-host model

What happened:

- `HASHI9` display improved
- `HASHI1` / `HASHI2` on the same WSL host still did not resolve to `same host`
- Display was improved for one topology but not fully generalized

Why this was wrong:

- It solved the symptom in one case without finishing the model
- It allowed another same-machine topology to remain broken

Required rule:

- Do not treat a topology-specific win as a generalized fix
- Same-host logic must be modeled once and reused consistently across all peer combinations

### 3.6 Validation was too UI-focused and not enough route-focused

What happened:

- Output text was checked
- The actual candidate host ordering and route resolution logic were not always revalidated alongside it

Why this was wrong:

- A nice message can still hide a bad route choice
- Display should reflect actual routing, not guess it independently

Required rule:

- Every UI change involving peer display must be validated against:
  - live peer payload
  - instances registry
  - candidate host ordering
  - actual route selection behavior

## 4. Current Confirmed Problems

This section lists the real issues confirmed as of this document.

### 4.1 `HASHI1` remote is currently not healthy from the local machine

Confirmed facts:

- `http://127.0.0.1:8766/health` timed out
- `HASHI2` sees `HASHI1` as offline
- The offline report is not purely cosmetic; the health probe is genuinely failing

Implication:

- There is a real liveness problem for `HASHI1 remote` right now
- The UI is not inventing that failure

### 4.2 Same-host detection does not properly cover `WSL ↔ WSL`

Confirmed facts:

- `HASHI9` and `HASHI2` are recognized as same-host
- `HASHI1` and `HASHI2` are not consistently recognized as same-host
- `HASHI1` has `host_identity = a9max` and `environment_kind = wsl`
- `HASHI2` is also on the same physical machine
- `same_host_loopback` is missing for `HASHI1` inside `HASHI2`’s registry state

Implication:

- Routing prefers LAN addresses where loopback should be preferred
- The display also becomes misleading because it reflects the wrong route model

### 4.3 Preferred-backend selection does not have a `WSL ↔ WSL` same-host path

Confirmed facts:

- `_select_preferred_backend` currently prefers `bootstrap_fallback` only when it is already loopback
- Otherwise it falls back to `lan`, then `tailscale`
- A `WSL ↔ WSL` peer commonly has only a `lan` observation
- That means the canonical peer host remains the LAN IP even if the peer is on the same machine

Implication:

- Writing `same_host_loopback` into `instances.json` is not sufficient by itself
- The routing model must either:
  - teach preferred-backend selection about same-host WSL peers
  - or explicitly override canonical host selection when same-host is proven

### 4.4 `same_host_loopback` persistence currently risks becoming write-only metadata

Confirmed facts:

- The earlier plan said to persist `same_host_loopback = 127.0.0.1`
- It did not explicitly name every consumer that must read this field afterward
- If the route-building path does not consume it, the field is decorative rather than functional

Implication:

- The plan must explicitly require route consumers to read `same_host_loopback`
- This includes the peer-routing path used by protocol traffic and any display layer that claims to show the chosen route

### 4.5 `live_status` can drift because it is not always derived through one path

Confirmed facts:

- Refresh success can directly stamp `live_status = "online"`
- Canonical rebuild later derives `live_status` again using age-window logic
- Those two paths can disagree temporarily

Implication:

- A peer can appear to jump between `online` and `stale` in a way that is technically explainable but operator-hostile
- State normalization must address data flow, not just wording

### 4.6 UI status wording is inconsistent with real status semantics

Observed example:

- A peer can be shown as red/offline
- The detail line can still say `handshake_in_progress`

Implication:

- The user sees contradictory state
- The UI is leaking internal implementation details instead of clear operator-level meaning

### 4.7 Peer display still leaks internal backend categories

Observed examples:

- `bootstrap_fallback`
- `handshake_inbound`

Implication:

- The UI exposes internal source labels instead of user-meaningful route categories
- This makes debugging harder, not easier, for the operator

### 4.8 Startup verification is still too narrow

Current behavior:

- `/remote on` verifies the local remote API came up
- It does not verify that peer state converged afterward

Implication:

- “Remote started” is correct for the local service
- But the operator may reasonably expect peer visibility to become correct immediately afterward

## 5. Non-Negotiable Rules Going Forward

These rules are mandatory. They are meant to prevent future time waste.

1. No claim of “fixed” without live verification on the real affected instances
2. No claim of “pushed” without matching `HEAD` and `origin/main`
3. No claim about a remote runtime version without checking that runtime’s actual repo `HEAD`
4. No claim about peer status without checking the live endpoint in the same debugging pass
5. No UI-only fix accepted if route-selection logic still disagrees
6. No topology-specific fix accepted as complete if another same-machine topology is still broken
7. No status report should omit uncertainty; if something is unverified, it must be called unverified

## 6. Remediation Goals

The remediation is complete only when all of the following are true:

1. Same-host routing is correct for:
   - `windows ↔ wsl`
   - `wsl ↔ wsl`
   - same-machine multi-instance topologies
2. Peer display reflects actual route selection
3. Operator-facing status wording is consistent and non-contradictory
4. `HASHI1 remote` is either:
   - genuinely healthy and shown online
   - or clearly shown offline with correct cause
5. Operational scripts report true process status
6. Validation procedure is documented and followed before any future “done” claim

## 7. Phased Fix Plan

### Phase 1 — Fix diagnostic tool trustworthiness first

**Priority:** P0  
**Goal:** Ensure process-control and status tooling can be trusted before deeper diagnosis

#### Code areas

- [`bin/bridge_ctl.ps1`](../bin/bridge_ctl.ps1)
- Any other operator-facing local diagnostic helpers discovered during the audit

#### Work items

1. Fix project-root resolution
2. Ensure process matching uses the real repo root and real `bridge-home`
3. Verify `status`, `stop`, and `restart` against live process tables
4. Audit other local diagnostic helpers used during this remediation to ensure they are not similarly misleading

#### Expected result

- The operator can trust script output again
- Phase 2 diagnostics are based on tools with known-good behavior

### Phase 2 — Diagnose and fix `HASHI1 remote` liveness failure

**Priority:** P0  
**Goal:** Determine why `HASHI1 remote` is not responding on `8766`

#### Code areas

- [`orchestrator/flexible_agent_runtime.py`](../orchestrator/flexible_agent_runtime.py)
- `HASHI1` runtime environment and startup path

#### Work items

1. Treat this phase explicitly as a diagnosis-first phase, not an assumed code fix
2. Build a decision tree for the current failure:
   - process not running
   - process running but bound incorrectly
   - process running and crashing
   - process running from the wrong checkout
   - process running under the wrong interpreter
3. Verify which repo checkout and Python interpreter `HASHI1` is actually using
4. Improve failure reporting so `/remote on` or equivalent restart flow captures:
   - command used
   - process ID
   - exit code
   - startup log tail
5. Re-test `HASHI1` health from the same machine after each candidate fix

#### Expected result

- `http://127.0.0.1:8766/health` either responds reliably
- Or the system can prove exactly why it does not

### Phase 3 — Fix same-host detection for all local topologies

**Priority:** P0  
**Goal:** Ensure same-machine instances are recognized correctly, including `WSL ↔ WSL`

**Precondition:** Before Phase 3 implementation begins, the route-normalization ownership decision must already be fixed:

- route-normalization fields are computed in the routing/control layer
- API server only serializes them
- UI/runtime only renders them

Phase 3 must not begin with that ownership still undecided, otherwise Phase 3 route work risks immediate refactor in the next phase.

#### Code areas

- [`remote/protocol_manager.py`](../remote/protocol_manager.py)
- [`remote/peer/registry.py`](../remote/peer/registry.py)

#### Work items

1. Before changing logic, perform a live check to prove whether `host_identity` is actually stable and equal across the relevant `WSL ↔ WSL` peers
2. Extend same-machine inference beyond `windows/wsl`
3. Add explicit support for `wsl/wsl` sibling instances on the same physical host
4. Use verified evidence such as:
   - `host_identity`
   - environment kind
   - known local roots
   - known Windows-visible WSL roots
5. Ensure `same_host_loopback = 127.0.0.1` is persisted for same-host WSL peers
6. Ensure same-host route consumers explicitly read `same_host_loopback`
7. Fix canonical host selection and/or preferred-backend selection so `WSL ↔ WSL` same-host peers do not remain stuck on the LAN host
8. Make loopback a first-class preferred route for same-host peers

#### Expected result

- `HASHI2` should prefer `127.0.0.1:8766` for `HASHI1`
- `HASHI1` and `HASHI2` should both display as `same host` from the relevant local perspectives

### Phase 4 — Unify route resolution and route display

**Priority:** P0  
**Goal:** Ensure the UI is rendering the actual chosen route, not reconstructing it loosely

#### Code areas

- [`remote/protocol_manager.py`](../remote/protocol_manager.py)
- [`remote/api/server.py`](../remote/api/server.py)
- [`orchestrator/flexible_agent_runtime.py`](../orchestrator/flexible_agent_runtime.py)

#### Work items

1. Implement the route-normalization architecture that was declared as a Phase 3 precondition:
   - route-normalization fields are computed in the routing/control layer
   - API server only serializes them
   - UI/runtime only renders them
2. Add normalized peer fields for operator display:
   - resolved route host
   - route kind
   - network host
   - same-host boolean
3. Return those fields directly from `/peers`
4. Stop exposing internal source names like `bootstrap_fallback` in the default UI
5. Render operator-friendly route summaries consistently

#### Expected result

- Same-host peers show:
  - `route: 127.0.0.1:<port> · same host · network: <lan-ip>:<port>`
- Normal peers show:
  - `addr: <host>:<port>`

### Phase 5 — Normalize peer state wording and state derivation

**Priority:** P1  
**Goal:** Remove contradictory combinations like “offline” plus “handshake_in_progress”

#### Code areas

- [`remote/peer/registry.py`](../remote/peer/registry.py)
- [`orchestrator/flexible_agent_runtime.py`](../orchestrator/flexible_agent_runtime.py)

#### Work items

1. Define a small operator-facing state model:
   - `online`
   - `starting`
   - `degraded`
   - `offline`
2. Make `live_status` derivation flow consistent:
   - successful refresh and canonical rebuild must not stamp conflicting meanings through different logic paths
3. Map raw handshake states into operator states
4. Show last error and last successful contact in a way that supports debugging
5. Keep raw handshake values available only for debug detail, not as the primary visible truth

#### Expected result

- The first visible state line should never contradict the detailed status explanation

## 8. Validation Plan

No phase may be marked complete without the validation items below.

### 8.1 Unit tests

Add or extend tests for:

1. Same-host detection:
   - `windows ↔ wsl`
   - `wsl ↔ wsl`
   - `wsl ↔ wsl` on different physical hosts
   - different hosts with similar network metadata
2. Candidate host ordering:
   - same-host loopback preferred first
   - LAN preferred for real remote peers
3. Peer rendering:
   - same-host route formatting
   - normal address formatting
   - offline/degraded/starting operator state formatting

### 8.2 Integration checks

Verify live combinations:

1. Phase 1 script-trust validation  
   Requirement: `bridge_ctl.ps1 status` output must be checked directly against the real process table and relevant listening ports; both views must agree before Phase 1 is considered complete
1. `HASHI2 -> HASHI1`  
   Dependency: Phase 2 must complete first if `HASHI1 remote` is currently unhealthy
2. `HASHI2 -> HASHI9`
3. `HASHI9 -> HASHI2`
4. `HASHI9 -> HASHI1`  
   Dependency: Phase 2 must complete first if `HASHI1 remote` is currently unhealthy
5. `HASHI2 -> MSI`
6. `HASHI2 -> INTEL`

### 8.3 Verification discipline

For every live verification pass, record:

1. Repo `HEAD`
2. `origin/main`
3. Remote instance repo `HEAD`
4. Remote process ID and creation time
5. Local health endpoint result
6. Peer list result
7. Rendered operator-facing summary
8. If a route claim is being made, the exact route evidence used to support it

### 8.4 Required proof before saying “fixed”

All of the following must be true:

1. The code is committed
2. The commit is actually on `origin/main`
3. The affected remote instances have actually pulled that commit
4. The affected runtimes are actually running after the update
5. The live health endpoints respond correctly
6. The live peer list reflects the expected topology
7. The proof bundle includes:
   - concrete `/peers` output excerpts
   - the actual route-selection result
   - a field-by-field mapping showing that the displayed output matches the route and health data

## 9. Phase Dependencies

The phases are not independent.

1. Phase 1 is a prerequisite for trustworthy diagnosis in Phase 2
2. Phase 2 may be a prerequisite for fully validating Phase 3 and Phase 4 in any flow that depends on a live `HASHI1 remote`
3. Phase 3 must finish before Phase 4 can be called topology-complete
4. Phase 4 should not invent its own route model; it must consume the architecture defined in Phase 3
5. Phase 5 depends on the data-flow changes from earlier phases if state wording is to remain stable

## 10. Exit Criteria

This remediation is complete only if:

1. `HASHI1` and `HASHI2` are correctly treated as same-host when appropriate
2. Same-host peers prefer `127.0.0.1` consistently
3. The peer list no longer shows contradictory state wording
4. The operator-facing route display is accurate and understandable
5. `HASHI1 remote` is either healthy or clearly and truthfully reported as unhealthy
6. Process-control scripts and other relied-upon local diagnostics are no longer misleading
7. No route-critical field is write-only metadata without a verified consumer
8. No future status update repeats the earlier verification mistakes

## 11. Summary

The remaining work is not cosmetic cleanup. It is a real remediation effort across:

- topology modeling
- preferred-backend selection
- route selection
- peer state semantics
- startup diagnostics
- operational verification discipline

The most important lesson is simple:

**Do not claim completion until the real affected instances have been proven healthy with current live data.**
