# HASHI Remote LAN observability — design delta (pre-code map)

Date: 2026-11-19 (Australia/Sydney). Branch: `feat/remote-lan-observability-20261119`.
Status: design delta recorded before code changes; the authoritative contract is
`docs/implementation/REMOTE_LAN_OBSERVABILITY_2026-11-19.md`.

## Files planned

New:
- `remote/api/lan_readiness.py` — bounded in-memory snapshot cache + bounded durable
  checkpoint. No timers, no threads, no I/O on read.
- `tests/test_remote_lan_readiness.py` — cache normalisation, redaction, reason-code
  whitelist, identity rejection, write suppression, staleness projection.
- `tests/test_remote_windows_lan_diagnostics.py` — deterministic PowerShell doctor
  tests with mocked NetSecurity/TCP cmdlets: missing rule, wrong profile, wrong port,
  explicit Block, GPO local-merge disabled, loopback-only bind, repair lifecycle,
  `doctor -Json` single-document contract, declined-UAC path (mocked elevation).

Modified:
- `remote/api/server.py` — `create_app` gains optional `lan_readiness_cache`; new
  `POST /lan-readiness/report` (loopback-or-HMAC, bounded body, schema-validated,
  never blocking); `/health` adds read-only `lan_readiness` projection in both views.
- `remote/main.py` — constructs the cache from the instance root and hands it to
  `create_app`. No new tasks or loops.
- `bin/hashi_remote_ctl.ps1` — `doctor` becomes precise: owned rules located by
  stable internal `Name` (`HashiRemote_<InstanceId>_TCP_<port>`,
  `HashiRemote_mDNS_UDP_5353`), ActiveStore effective policy, explicit-Block
  evidence, listener PID/bind ownership, legacy DisplayName matches as context only.
  `doctor -Json` emits exactly one JSON document on stdout; `doctor -Repair` is the
  only path that creates/updates owned rules (narrow Domain/Private + LocalSubnet,
  UAC elevation bounded to one relaunch, mDNS refcount kept in the rule Description,
  unrelated rules never touched). After a successful doctor/repair the snapshot is
  pushed once to the local Remote via `POST /lan-readiness/report` (best effort).
- `docs/HASHI_REMOTE_PROTOCOL_SPEC.md` — documents the additive
  `lan_readiness.schema_version:1` contract and stable reason codes.

## Field schema (pinned)

`lan_readiness` on `/health` (additive; older consumers ignore it):

```json
{
  "schema_version": 1,
  "state": "degraded|ok|unknown|disabled|unavailable",
  "observed_at": "<ISO-8601 or null>",
  "stale": false,
  "source": "windows_doctor|none",
  "process": {"state": "ok", "pid": 1234, "supervised": true},
  "listener": {"state": "ok", "address": "0.0.0.0", "port": 8767, "pid": 1234, "process_name": "python.exe"},
  "firewall": {"state": "ok|degraded|unknown", "reason_code": "...", "effective_policy_checked": true},
  "mdns": {"advertising": "ok|degraded|unknown", "browsing": "ok|degraded|unknown", "external_visibility": "unknown"},
  "external_reachability": {"state": "unknown|ok|degraded", "reason_code": "lan_reachability_unverified"},
  "reason_codes": ["..."]
}
```

Reason codes: only the stable set from the master plan (`firewall_rule_missing`,
`firewall_rule_disabled`, `firewall_profile_mismatch`, `firewall_remote_address_mismatch`,
`firewall_rule_port_mismatch`, `firewall_explicitly_blocked`,
`firewall_local_rules_ignored_by_policy`, `firewall_policy_unverified`,
`loopback_only_bind`, `listening_port_mismatch`, `port_owned_by_other_process`,
`remote_process_down`, `diagnostics_unavailable`, `mdns_advertise_failed`,
`mdns_browse_failed`, `mdns_rule_missing`, `mdns_visibility_unverified`,
`lan_reachability_unverified`, `tcp_connection_timeout`, `tcp_connection_refused`,
`tls_handshake_failed`, `authentication_rejected`, `identity_mismatch`,
`gateway_unavailable`). Unknown codes from future reporters are preserved verbatim;
reporters never invent codes outside this list.

## Owner of each observation

| Observation | Owner | Recompute trigger |
| --- | --- | --- |
| process / listener | Windows doctor (PowerShell), pushed to Remote cache | explicit `doctor` / repair; else `unknown` |
| firewall policy | Windows doctor, ActiveStore read | explicit `doctor` / repair only (never on `/health`) |
| mDNS advertise/browse | existing discovery backends + doctor projection | existing discovery lifecycle (unchanged) |
| directed route reachability | existing liveness/handshake owner + peer registry | existing owners only; `last_seen_ok`/failure tier already persisted |
| gateway | Workbench (separate repo) | Workbench probes; HASHI never fabricates it |

`/health` and `/protocol/status` remain pure reads of the in-memory snapshot; no
PowerShell, firewall scan, network probe or file parse is added to any request path.

## Expected new schedules

**Zero** new timers, tasks, polling owners, threads or daemons. The snapshot is
refreshed only by explicit doctor/repair pushes. Staleness is derived from
`observed_at` (stale after 3600 s) so a stale panel can never masquerade as live proof.

## Persistence

`<hashi_root>/state/lan_readiness.json`, written only on a material snapshot change
or at most every 600 s for identical healthy state, mirroring registry semantics.
Reads on `/health` never touch the file.

## Testing environment and risks

Environment: native Windows host (`a9_max`), HASHI3 venv Python 3.12 for the
isolated LANLAB1 test instance on port 8779; real `Get-NetFirewallRule` inspection
is read-only in the live doctor validation run; no firewall rule is created on this
machine without the explicit `-Repair` path, which is tested only with mocked
elevation (real UAC prompts are left to Barry's acceptance step). Two-machine
acceptance is not available on this host and will be reported as not executed.

Risks: (1) PowerShell 5.1 JSON depth/quoting constraints — tests pin the
single-document contract; (2) ActiveStore effective-policy shape differences —
the doctor reports `firewall_policy_unverified` instead of guessing; (3) repair
must never open Public or Any remote address — covered by mocked tests and the
narrow rule template.
