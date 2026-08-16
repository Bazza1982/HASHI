# HER `/rebuild` Development Takeover and Completion Record

Date: 2026-08-16

Development branch: `feature/her-rebuild-command`

Integrated branch: `agent/latest-hashi-her`

Final Zelda HASHI base: `5776c27882b7e024e442f6c9dcc44901eede0a59`

Final certified HER source: `246b04e9fa28ef0b6f74c2d924ab3697b95197bd`

Implementation commits:

- `57eb8ea3` — isolated rebuild controller foundation;
- `c20cd08d` — integrated Rust source and supervised development rebuild
  workflow.

Primary integration commits:

- `96aa4fe1` — isolated rebuild controller foundation;
- `6c7fd961` — integrated Rust source and supervised development rebuild;
- `bf8766ca` — verification documentation;
- `2175e0bb` — first-hot-reload stable-manager adoption and non-HER guard.

Status: implementation, primary integration and live HER adoption complete

## 1. Safe takeover

The primary HASHI worktree and Zelda's HER source worktree were audited before
integration.

Zelda's final work was fully committed:

- HASHI `.22` package, certification and live evidence ended at `5776c278`;
- HER source branch `release/her-0.1.0-hashi.22` was clean at certified tag
  `her-0.1.0-hashi.22-certified-r2`;
- Linux and Windows certified binaries retained their recorded hashes.

Before rebasing, the prior feature state was preserved in a Git bundle and the
untracked older plan copy was archived. The feature branch was then rebased
onto Zelda's final HASHI commit. No Zelda file was reset, overwritten or
discarded.

During final verification, a concurrent live request changed
`tools/telegram_send_file_cli.py` to accept a UTF-8 BOM. The original backup
was first checked byte-for-byte against the prior Git version and moved to the
ignored takeover archive. The valid one-line change was preserved in its own
commit (`c9dfeb53`) before this feature was integrated. No concurrent file was
overwritten or discarded, and the primary worktree was clean before every
feature cherry-pick.

## 2. Integrated Rust source

The certified source was imported under `native/her` with:

- upstream MIT licence;
- exact source commit, branch and certified tag;
- certified source-bundle path, size and SHA-256;
- explicit record of HASHI-local changes;
- a dedicated `[profile.hashi-dev]` incremental Cargo profile.

Tracked upstream interactive sessions, local sandbox settings and agent
workflow state were deliberately excluded because they are not build source.
The production `.22` package remains separate and unchanged.

## 3. Implemented runtime path

The finished development transaction is:

```text
authorized /rebuild
-> source/toolchain fingerprint
-> same-fingerprint join or single OS build lock
-> isolated incremental Cargo build
-> offline candidate verification
-> immutable candidate and digest evidence
-> wait for the requesting Agent to become fully idle
-> atomic development selection
-> existing targeted hot restart
-> Adapter adoption and backend health check
-> success, or one automatic selection/restart rollback
-> idempotent terminal notification
```

The manager is kernel-owned and intentionally excluded from the hot-manager
bundle, so it survives the targeted restart it requests. The manager registry
also installs it when an already-running pre-feature kernel performs its first
`/reboot`; a cold process restart is not required merely to expose the new
command.

Cold process interruption is also fail-safe: pre-activation jobs become failed;
an interrupted selection/adoption is restored before Agents start.

## 4. Safety boundaries

- `/reboot` continues to reload Python/config and does not invoke Cargo.
- `/rebuild` builds only the current host development target.
- Build environment variables are allowlisted; provider and Telegram secrets
  are not passed to Cargo or quick probes.
- Candidates are stored outside the tracked source tree and are immutable after
  digest recording.
- A selected development candidate is validated by location, target,
  executable mode, metadata and SHA-256.
- An invalid explicit selection fails closed and never falls through to PATH.
- A build/probe failure leaves the active HER unchanged.
- A busy Agent causes `activation_deferred`; no force-activation option exists.
- Only the requesting Agent is automatically restarted in the normal one-user
  path.
- A live Agent using a backend other than HER is rejected before Cargo starts.
- Certified package manifests, binaries, evidence and baselines are never
  changed by this workflow.

## 5. Recorded validation

Focused and expanded Python suites:

```text
Rebuild/Adapter/command focused suite: 162 passed
Expanded HER integration suite:        358 passed
Feature-branch HASHI suite:             2237 passed, 3 skipped, 23 warnings
Final integrated HASHI suite:           2278 passed, 2 skipped, 23 warnings
```

The 23 Python warnings are existing python-telegram-bot `retry_after`
deprecation warnings.

Native Rust CLI:

```text
cargo test --locked --profile hashi-dev -p rusty-claude-cli
358 passed, 0 failed
```

This covers unit, CLI/config, compact output, mock-provider parity,
output-format/version/doctor/stream-json contracts and resume/session behavior.
The compiler emitted the existing unused-variable warnings in
`output_format_contract.rs`; there were no failures.

Real host development build:

```text
Clean first build:     71.734 seconds
Unchanged-source path: 0.398 seconds
Candidate ID:          dev-a63e2ab0cf29425c-85e5e5a1b647
Candidate SHA-256:     85e5e5a1b647ca28eb24b2d09de741202e8fa198e69fbc455a983cc0e26be033
Embedded Git SHA:      c20cd08da2200b28e25b38c452e797688092af7a
Source dirty flag:     false
```

Mandatory offline checks passed for version identity, target, doctor, stdin CLI
contract and deterministic stream-json framing.

## 6. Canary and rollback evidence

The first controller canary intentionally used a nonexistent target Agent.
It built and verified the real binary, then correctly ended in
`activation_deferred`. There was no active development selection and no Agent
restart.

A separate real resolver drill then:

1. atomically selected the immutable candidate;
2. resolved it as `development-source-build` even under
   `require-packaged`;
3. restored the previous selection;
4. resolved the certified Linux `.22` package again.

The certified binary hashes remained:

```text
Linux  e6c88b9dd37c9191f9aad0df9fd0cf9bbeb4365778a10153a48b4cf752096c91
Windows cd127b283d0bb8aa5db9d1863a617bb84a2c8cd0174ed305c72c5f97b294724d
```

## 7. Primary live canary

The running HASHI kernel initially predated `/rebuild`. A local authorized
`/reboot min` against idle Sunny rebuilt the hot managers, installed the new
stable rebuild manager and returned Sunny to HER/online in about five seconds.
`/rebuild status` then proved that the dynamic command and durable manager were
available without a cold kernel restart.

The first real primary-branch `/rebuild` produced:

```text
Job:                    rebuild-20260816-090741-d3151617
Source fingerprint:     0ee10120fd7564e02d22f856b4a868e7ac014b9791dd60eb16f34051c195e7e4
Cargo build:             65.171 seconds
End-to-end transaction:  81 seconds
Candidate:               dev-0ee10120fd7564e0-61c151ebf1ec
Candidate SHA-256:       61c151ebf1eca6b1ad46739f3ad1f78dad616cc45451d53ae324b5c29a6a17d1
Embedded Git SHA:        2175e0bb34d9c46b73aeee6157624587efef0ad4
Result:                  succeeded / adopted / terminal delivered
```

The persisted selection points to that immutable candidate and labels it
`development_build=true`, `production_certified=false`. Independent SHA-256
calculation matched the stored digest. Version identity, target, doctor,
stdin/CLI and stream-json checks all passed.

The second same-source job (`rebuild-20260816-090952-a2852392`) recorded
`candidate_reused=true`, launched no Cargo process and completed the complete
verification/restart/adoption transaction in 15 seconds.

## 8. Operator surface

```text
/rebuild
/rebuild status
/rebuild status <job-id>
```

For build-and-verify-only local use, without selection or restart:

```text
python scripts/her_rebuild_dev.py
python scripts/her_rebuild_dev.py --status
```

The command returns an accepted/joined job identity immediately. Cargo runs in
the background. A material terminal result reports success, deferred
activation, failure, successful rollback or manual-reconciliation-required
rollback failure.

The full authoritative design and acceptance criteria remain in
`HER_REBUILD_COMMAND_IMPLEMENTATION_PLAN.md`.
