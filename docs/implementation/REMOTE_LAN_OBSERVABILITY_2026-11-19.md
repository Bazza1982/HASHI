# HASHI Remote LAN observability and firewall lifecycle — implementation contract

Date: 2026-11-19 (Australia/Sydney). Status: **planning-only handover**, not an implementation or deployment.
Repository: `Bazza1982/HASHI`.
Work branch: `feat/remote-lan-observability-20261119`, forked from `main` at `682f0fe997d625f44ad7144644db768bebf69e15`.
Companion: `Bazza1982/hashi-workbench-v2` / `feat/remote-lan-workbench-20261119`.
Companion plan: https://github.com/Bazza1982/hashi-workbench-v2/blob/feat/remote-lan-workbench-20261119/docs/implementation/REMOTE_LAN_WORKBENCH_2026-11-19.md

## 0. Goal and definition of done

A HASHI Remote can be running locally yet invisible to another host. Diagnose which layer fails, retain useful known-instance identity and last-verified route, and offer an explicit Windows firewall repair. Do not equate a green local /health response, a firewall rule, a multicast announcement, or a shared token with demonstrated remote reachability. Preserve functional routes even if another discovery/diagnostic subsystem is degraded.

**Hardest requirement: no recurrence of high-frequency wake-ups, power draw, cold-start/reboot stalls, status-request side effects, filesystem churn, log floods, repeated network probes or topology storms.** Correctness, availability, and idle cost are all release gates.

This task does NOT authorise changes to HASHI Core, HER/HERV3, Telegram, agent protocol semantics, unrelated security/permission systems, production instances, main, or running Windows services. It does not authorise changing branch protection, triggering paid GitHub Actions, merging, or deployment.

## 1. Verified baseline — do not accidentally undo it

Inspect these files at branch HEAD before changing code:
- `bin/hashi_remote_ctl.ps1`: `doctor` currently matches inbound rule DisplayName by `Hashi|Remote|Python`, counts unrelated rules, makes a local HTTP health probe, prints `Format-List` and exits non-zero according to local health acceptance. Preserve the existing human-use contract while adding precise diagnostics and machine-readable output.
- `remote/main.py`, `remote/api/server.py`, `remote/peer/lan.py`, `remote/peer/registry.py`, `remote/peer/tailscale.py`: existing OS-independent Remote, discovery, trust and peer stores. Extend existing owners rather than creating a second registry/service.
- `docs/HASHI_REMOTE_PROTOCOL_SPEC.md`: discovery, trust, readiness, remote health and read-only projection definitions.
- `docs/repairs/REMOTE_TELEGRAM_DECOUPLING_2026-11-16.md`: **binding performance baseline and ownership constraints**.
- `tests/test_remote_windows_local_readiness.py` and `tests/test_remote_windows_supervisor.py`: local-ready must not depend on arbitrary neighbor trust/discovery failures.
- Companion Workbench already calls `reconcileDiscovered(..., {pruneMissing:false})` on this baseline; do NOT plan an unnecessary rewrite or re-enable pruning.

Existing effective contracts from 2026-11-16:
- Protocol control idle wake ~30 seconds (observed 55 wakes in a 30-minute soak), Core/directory at most 2/min, peer liveness 0.5/min, accepted-handshake revalidation 0.2/min, bootstrap 0.2/min. Active in-flight work alone may use 0.5s.
- Discovery maintenance owner ~30 seconds / 2 cycles per minute; no duplicated agent-directory work; unchanged metadata must not force advertisement.
- Tailscale discovery single 60-second owner, backing off to 300 seconds after failure.
- Registered peers are rechecked by existing liveness/handshake owner, at most one eligible address per peer/pass; address/peer circuit/backoff is preserved (including 30–300s peer backoff).
- No repeated file writes for unchanged heartbeats/discovery/errors; durable healthy state checkpoint no more often than 600 seconds.
- Route selection, Remote /health, Workbench health and Core startup must be non-blocking, read-only projections; local Worker/Core readiness is independent of Remote and Telegram.
- Successful gateway traffic must not regenerate ~10 INFO records/request.
- Do not regress the existing independently supervised Remote process or any Windows supervisor-log bounds.

## 2. Architecture: independent observations, not a serial gate

There are three different scopes; state must carry a `source` / observation direction and `observed_at`:
1. **Local instance (HASHI owner)**: process/supervisor, bound address+port+PID, effective firewall policy, mDNS advertise/browse, local health.
2. **Directed remote route (existing liveness owner)**: source host -> target instance/address:port; TCP, TLS if configured, HMAC, expected instance-ID, last-verified endpoint, last success/failure, whether actually tested.
3. **Workbench gateway (Workbench owner)**: authenticated Remote route -> local Workbench gateway/API. Do not infer a gateway failure from a TCP failure or vice versa.

mDNS is an optional route-discovery path alongside trusted saved routes, manual address candidates and authenticated /peers. Shared token authenticates *after* locating a host; it is not discovery. One route can be working while multicast discovery is degraded.

Use orthogonal values such as `ok`, `degraded`, `unknown`, `disabled`, `unavailable` plus stable `reason_code`, not a single fail-closed chain. A local-only Remote may retain `status:ready` if that means local service ready; separately expose `lan_readiness` as unverified/degraded. A timestamped external probe is required to call a particular directed route reachable. An unverified LAN is **not green** as a LAN status.

## 3. Versioned minimal diagnostics contract (additive)

Add optional, backward-compatible `lan_readiness` on existing Remote `/health`; do not rename/remove current top-level or `discovery` / trust keys. Suggested wire shape — exact naming may be refined jointly with the Workbench agent before coding, then pin in tests:

```json
{
  "status": "ready",
  "lan_readiness": {
    "schema_version": 1,
    "state": "degraded",
    "observed_at": "2026-11-19T00:00:00Z",
    "stale": false,
    "source": "local_snapshot",
    "process": {"state": "ok"},
    "listener": {"state": "ok", "address": "0.0.0.0", "port": 8767, "pid": 1234},
    "firewall": {"state": "degraded", "reason_code": "firewall_rule_missing", "effective_policy_checked": true},
    "mdns": {"advertising": "ok", "browsing": "ok", "external_visibility": "unknown"},
    "external_reachability": {"state": "unknown", "reason_code": "lan_reachability_unverified"},
    "reason_codes": ["firewall_rule_missing", "lan_reachability_unverified"]
  }
}
```

**Critical implementation detail:** `/health` must only read a pre-computed, bounded in-memory snapshot. It MUST NOT call PowerShell/`Get-NetFirewallRule`/network APIs, probe a peer, reread files, synchronously refresh topology, restart the supervisor or schedule extra work on every request. No timer may exist solely to keep this panel green. If firewall policy has never been explicitly checked, return `unknown` with a reason and age; do not claim checked. Recompute only on explicit doctor/repair, existing lifecycle operations, or an already-owned low-frequency/config-change opportunity with a measured non-blocking implementation. Remote process-down cannot be served from that dead process; an OS/local controller reports it.

Stable reason codes (distinguish fact vs inference; only return supported codes):
- Local: `remote_process_down`, `loopback_only_bind`, `listening_port_mismatch`, `port_owned_by_other_process`, `diagnostics_unavailable`.
- Firewall: `firewall_rule_missing` (missing HASHI-owned rule, NOT proof no other allow rule), `firewall_rule_disabled`, `firewall_profile_mismatch`, `firewall_remote_address_mismatch`, `firewall_rule_port_mismatch`, `firewall_explicitly_blocked` (only with matching effective-policy evidence), `firewall_local_rules_ignored_by_policy`, `firewall_policy_unverified`.
- Discovery: `mdns_advertise_failed`, `mdns_browse_failed`, `mdns_rule_missing`, `mdns_visibility_unverified`. Failed external observation is not proof that local publishing failed.
- Directed route: `lan_reachability_unverified`, `tcp_connection_timeout`, `tcp_connection_refused`, `tls_handshake_failed`, `authentication_rejected`, `identity_mismatch`, `gateway_unavailable` (the last is Workbench-observed, not fabricated by local HASHI).

Separate `severity` from `confidence/evidence`: timeout suggests firewall, sleep, Wi-Fi isolation, VLAN or routing but cannot claim a firewall block without host evidence. Use `not_tested` when TCP did not reach HMAC; never label token rejected after a timeout. Error strings never contain shared token, HMAC proof, private keys or raw credential material. Ensure schema has forward-compatible unknown values.

## 4. Windows doctor: read-only, deterministic and precise

Extend `bin/hashi_remote_ctl.ps1 doctor` without breaking present human output/exit behavior unless explicitly versioned. Provide a documented machine mode such as `doctor -Json` or `-OutputFormat Json` that emits exactly one clean JSON document to stdout and directs warnings to stderr. Include schema version, timestamp, instance ID, effective configured port, actual bind addresses, listener PID/process ownership, network profiles/interfaces and source evidence.

Do not match DisplayName with `Hashi|Remote|Python`. Locate HASHI-owned rule by stable internal `Name` / group / instance ownership metadata and verify:
- Enabled, Inbound, Allow/Block, Protocol TCP/UDP, LocalPort, profile, address restrictions (`Get-NetFirewallPortFilter` / `Get-NetFirewallAddressFilter`), and relevant interface/local address.
- `Get-NetFirewallRule -PolicyStore ActiveStore` and `Get-NetFirewallProfile -PolicyStore ActiveStore`, including local-rule merge and effective inbound-rule policy. An explicit effective Block or applicable GPO/MDM can override a local Allow; do not silently rewrite administrator policy.
- The actual listening PID matches the expected Remote instance; distinguish 127.0.0.1 / ::1, wildcard IPv4 / IPv6 and a routable LAN interface. Account for VPN/Tailscale/WSL and multiple NICs without pretending all paths are same-subnet.
- Existing legacy path-bound Python rules as diagnostic context only, not mistaken for HASHI-owned current allow rules.
- Discoverability facts from existing mDNS state; do not infer packet visibility from a local flag.

If running without enough rights, return `diagnostics_unavailable`/unknown for privileged observations, not invented success; continue other checks. Do not run repair through doctor, status, health, background startup or GET. A real remote host probe must originate from that external host to prove its directed TCP route.

## 5. Firewall lifecycle and repair (explicit, isolated)

Define a small idempotent Windows-specific firewall helper/command; do NOT infect Linux/macOS startup. Design stable internal names that encode instance identity and rule purpose; visible `DisplayName` may be 'HASHI Remote MSI TCP 8767' but must not be the sole ownership key. Annotate owned rule with purpose + instance ID + repository/home identity, and never remove unrelated rules.

- Instance-scoped TCP inbound allow for *actual configured* Remote port. Profile Domain/Private, remote address LocalSubnet by default, Public never auto-open. Restrict local address/interface where applicable and consider IPv4/IPv6. No Python executable-path dependency; document that pure port rules can apply to another program that binds that port.
- Host-scoped shared UDP 5353 mDNS rule on Domain/Private, using one clear ownership/refcount strategy for multi-instance installations. Verify multicast traffic and both advertisement and browsing separately; opening a port is not proof a router forwards multicast.
- Tailscale/cross-subnet reachability is an explicitly separate transport/policy; do not weaken normal LAN rule to Any or create an internet-facing exception.
- Install / first enable / user-selected repair may prompt for explicit consent and UAC on the affected *local* Windows machine. Scheduled background process is Limited and must not elevate, prompt, spin, or fail Core/Remote startup because UAC is refused.
- On upgrade, start, network-profile change and configured port change: compare cached/configured desired state with actual state when the already-authorised lifecycle or explicit doctor action runs. Do NOT poll effective firewall policy on the hot path.
- On port mismatch, expose `firewall_rule_port_mismatch` and present targeted repair. Do not silently open a new random port; do not erase a previous owned rule before a replacement's success is verified. Preserve old working path until replacement is safe where compatible.
- Repair only the exact owned rule(s) and only with explicit user action; recheck and report effective policy; if GPO prevents it, return useful guidance. Uninstall/unregister removes only recorded HASHI-owned rules, and never removes host-shared mDNS while another HASHI instance still uses it. Failure to repair is a diagnostic state, not a global startup blocker.
- Public profile must not be called “localhost-only” merely because no HASHI-owned inbound allow exists: check effective policy or bind loopback explicitly for that mode. No blanket firewall disable, global profile change or policy bypass.

## 6. Discovery and peer persistence — reuse existing owners

Enhance existing `remote/peer/lan.py` and peer registry rather than adding another scanner/DB/worker:
- Expose advertise/browse startup errors and dynamic health; include DNS-SD TXT length and total payload bounds (prior 255-byte single-value failure must remain covered).
- Distinguish an advertisement constructed locally from a different host receiving it; external mDNS visibility remains unknown unless peer observation proves it.
- Persist successfully authenticated identity and the latest verified route(s), `last_verified_at`, and the latest material failure tier; preserve failed/unknown offline peers. The durable trust identity is `instance_id`, not mutable IP/port/host name.
- Candidate addresses from mDNS, manual entry, saved peer or authenticated `/peers` are untrusted hints until *existing HMAC and expected instance identity* succeed. Shared token is not a second discovery or pairing mechanism; keep required identity/HMAC checks without adding scopes, owner gates or new auth layers.
- Retain prior tried addresses, implement bounded failover with existing circuit breakers. **One eligible address per existing peer liveness/handshake pass**, never a full subnet port scan or eager fan-out. Missing multicast must not demote a verified direct-IP route.
- Persist only structural/trust/online-offline transitions; skip identical snapshots/diagnostic churn. Keep existing <=600s checkpoint semantics. No per-heartbeat or per-status file writes, new SQLite write hot loop, or status/error INFO spam.
- Peer information consumed by Workbench is a read-only projection and must not trigger liveness refresh.

## 7. Phased implementation and cross-repository interface

H0. Record exact branch HEADs, existing tests/counters, current field names and Windows firewall rule inventory. Reconcile conflicting historical documents: Workbench current code has `pruneMissing:false`.
H1. Agree and test one small, versioned health/doctor reason-code schema. Consumer must tolerate absent fields and unknown codes. Do not change core / existing HMAC wire contract.
H2. Implement pure firewall-rule matching and local state classification, then read-only `doctor -Json`. Mock NetSecurity cmdlets and effective-policy combinations in deterministic tests.
H3. Implement explicit local UAC repair lifecycle and precise owned-rule cleanup. Add PowerShell mocking tests and real Windows validation, avoiding unintended UAC in automation.
H4. Project lightweight cached diagnostics into Remote `/health`, reuse existing discovery/peer owners; no extra polling. Add last-verified and material failure tier using existing persistence.
H5. Hand off schema fixtures to companion Workbench branch; test older/newer combinations, network outage and nonblocking status reads; document rollback.
H6. Open Draft PR for review only after local tests/evidence. Do not merge or deploy.

## 8. Acceptance matrix — no synthetic green

Required functional tests (negative and recovery):
1. Clean Windows Private/Domain install; firewall missing/disabled/profile mismatch/remote address mismatch; explicit Block; GPO local-rule merge disabled; Public/declined UAC; no blanket exceptions.
2. Changed Python runtime path (port rule remains valid); changed Remote port and occupied port; two Remote instances sharing one machine and UDP 5353; uninstall one while other remains.
3. Loopback-only bind vs wildcard; IPv4 and IPv6; Wi-Fi/Ethernet/VPN/WSL/Tailscale; mDNS blocked but direct IP works; multicast never externally observed; broken DNS-SD TXT / zeroconf startup.
4. TCP refused/timeout, TLS failure, wrong token, identity mismatch, Gateway down while Remote reachable, offline peer persisted and recovered after IP reassignment without stale-IP identity adoption.
5. A -> B succeeds while B -> A fails; real *two-machine* source-labelled verification. Local doctor without peer proof returns unverified, not green.
6. Remote unavailable during startup or /reboot max: Core/Workers/local Workbench remain ready; optional diagnostic exceptions never add a blocking path.
7. Failure and restart: previous user-owned rules and peer snapshots are not destroyed; errors redacted; no repeated notifications or INFO amplification.

## 9. Mandatory idle/power/latency regression budget

Capture a **before and after** run on same hardware, same 2+ peer topology, same configuration, with a 30-minute *no user activity* phase and a separate controlled outage/recovery phase. Use existing in-memory monotonic counters, not a new sampling daemon. Measure process CPU time, wake-ups, network probes/minute, HTTP and gateway calls, registry writes, disk bytes, INFO logs, Core/Workbench p50/p95 local response, FC ready and /reboot max duration. Record numbers and commands in PR.

Absolute code-level prohibitions:
- **Zero new periodic tasks, timers, polling owners, background threads or daemons** in idle steady state; no shortening any current interval. Explicit button presses may make one bounded single-flight diagnostic request.
- Zero firewall/PowerShell/remote TCP probe or filesystem parse performed by routine `/health`, backend-selector rendering, Core readiness or message hot path.
- Existing 30s idle protocol + discovery cadence, 120s liveness, ~300s handshake/bootstrap, 60–300s Tailscale and <=600s unchanged persistence constraints remain intact. Do not turn event notification into a wakeup storm.
- At most one endpoint attempted per peer maintenance pass; preserve route circuit/backoff; no broad network scan, no “retry every status request”.
- For unchanged state, no newly added disk writes/log records, repeated JSON serialization, token rereads, topology publications or gateway calls.

Suggested diagnostic reference from 2026-11-16 live 30-minute soak: protocol idle wakes 55, liveness passes 14, handshakes 6, bootstraps 6, discovery maintenance 60, unchanged advertisements 0, durable registry checkpoints 3. These are **comparison evidence**, not universal absolute thresholds; compare within the same environment and document test-phase offsets and jitter. A code review must still prove no extra owner/timer. Require no material increase over baseline in steady-state probes/calls/CPU/writes and no statistically meaningful p95 latency or startup regression. If idle/power evidence is unavailable, PR remains Draft / performance not verified; never mark it passed by inference.

## 10. Local agent deliverables, stop conditions and rollback

- Work only on `feat/remote-lan-observability-20261119`; do not merge/rebase main without first verifying SHA and discussing overlaps. Preserve dirty local worktrees, do not reset, do not touch installed/production Remote or firewall until explicit test approval.
- Before coding, write a one-page design delta: planned files, exact field schema, owner of each observation, potential new schedules (expected **none**), testing environment and risks. If any implementation needs a new polling loop, startup wait, new auth system, broad firewall policy or Core modification: **stop and ask Barry**.
- Implement narrow commits for schema/tests, doctor, explicit repair, projected peer health; keep companion Workbench in sync via fixtures, not by changing its repository without coordinating.
- Run focused local/offline Python + PowerShell tests and targeted startup/Remote performance tests. No paid GitHub Actions runs/re-runs unless Barry separately approves. Report failures/skips honestly, especially Windows/UAC/two-host tests that were not available.
- When local code and tests are ready: open a **Draft PR** referencing the Workbench branch/PR, attach baseline-vs-after counters, negative test matrix, exact commit SHA, manual Windows acceptance still required, rollback commands and risk list. Do not auto-merge, deploy or restart production.
- Rollback must restore previous diagnostics projection and owned firewall-rule state without disabling Windows Firewall or removing unrelated rules; old clients must keep using legacy /health and Workbench connection contracts.

## 11. Authoritative references

- HASHI performance baseline: `docs/repairs/REMOTE_TELEGRAM_DECOUPLING_2026-11-16.md`
- HASHI protocol contract: `docs/HASHI_REMOTE_PROTOCOL_SPEC.md`
- Windows PowerShell: Microsoft NetSecurity Get-NetFirewallRule/PortFilter/AddressFilter/Profile, ActiveStore effective policy, and explicit local UAC.
- mDNS: RFC 6762; DNS-SD TXT size: RFC 6763.
- Companion plan: `Bazza1982/hashi-workbench-v2` work branch, link at top.

**Final guardrail:** The solution must explain broken LAN without making a healthy local HASHI, healthy saved direct route or Workbench UI slower or less available.