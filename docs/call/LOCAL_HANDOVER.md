# /call: paired-branch local takeover

Both HASHI and Workbench use `feature-call`. This is source ready for local
qualification, not an already-deployed or fully qualified production release.
Read `IMPLEMENTATION.md` and the PRD in this directory before editing.

## Safe checkout and scope

Find the actual two checkouts from their Git remotes and identify the intended
HASHI instance, selected Agent, configuration and Workbench process. Do not guess
an instance from a screenshot. Record existing branch, HEAD and dirty files.
Fetch `origin feature-call` in both repositories; use clean worktrees when existing
work is dirty or already running. Do not reset, stash, rebase or overwrite another
agent's work without permission. Record both checked-out SHAs as a paired test.

Keep Workbench private. Do not copy its source, screenshots, credentials or user
recordings into HASHI's public repository. No main merge, deployment, protected
Core migration, or broad restart is authorized by pulling this feature.

Functional owner: Frontend Connector Functions. PAO retains Message/Run/Session
ownership. `/phone` transport/settings remain independent; its only new backend
behavior is the foreground-call interlock. One front-end call owns devices at a
time. The normal single-recording microphone is separate.

## Focused offline checks

HASHI, in an isolated environment compatible with the repository runtime:

```bash
python -m pytest -q tests/frontend_call tests/test_live_voice_integration.py tests/test_architecture_boundaries.py
python scripts/check_protected_core_changes.py --base 6a4b29b881dd3077c1c2302e4bbc6e41b0277bf9
git diff --check
```

Workbench, Node 22:

```bash
npm ci
node --test src/features/call/call.test.js src/features/call/callReact.test.js src/features/live-call/liveCallPolicy.test.js src/features/live-call/liveCallApi.test.js server/request_privacy_policy.test.js
npm run build
git diff --check
```

The first remote checks passed 84 Python tests and 44 Node/React tests. The
Workbench production build passed. These do not qualify live devices, real
provider credentials or running Function generations. `npm ci` also reported
one high-severity advisory in the unchanged lockfile; inspect `npm audit`, assess
its exposure, and propose a focused fix separately. Never use `audit fix --force`
or unreviewed broad upgrades as a test shortcut.

## Configure a bounded canary

Keep `/phone` enabled and its settings unchanged. Back up ignored instance files.
In the test Workbench set `HASHI_WORKBENCH_CALL=1`. On the HASHI host, copy the
example profile beside `api.config_path`, not to an arbitrary repository root:
`examples/call_profiles.example.json` -> ignored `call_profiles.json`.

Replace all example URLs/model/voice IDs with actual allowed endpoints. This
release ships OpenAI-compatible protocol adapters, not every vendor protocol.
Select STT, TTS and optional vision independently; local means loopback on the
HASHI host. Store keys in Function environment variables, never browser JSON.
Do not download models, create accounts or incur unapproved API usage. Reuse
available approved endpoints; ask only for missing credentials or required budget.

Adopt both the shared Backend API Functions and selected Agent Functions through
the existing supported generation mechanism. Ask before `/reboot max` or any
operation affecting unrelated Agents. Never cold-restart or modify protected Core
for this feature. An old privacy metadata projection intentionally blocks cloud
media. Level 2/unknown privacy blocks raw cloud media; do not disable that guard
just to make a test pass.

## Minimum live acceptance, after scope approval

Use one short English turn, one Chinese turn and one mixed technical turn. Confirm
one normal Message/Run per utterance, text persistence and sequential TTS. Record
actual latency; do not infer price or quality from cloud/local location alone.
Verify the narrow floating card, drag, minimize/restore, retained sidebar and
usable composer. Captions are batch turn captions, not streaming transcription.

Enable camera explicitly, test denial, pin a frame and move the object before
speaking. The selected snapshot must match the question. Closing camera/end must
release tracks. The Agent has a static avatar, not generated video. Test stop
playback, mute, end while reasoning, switching Agent view, context/connection
changes, expired leases and one TTS retry. No automatic replay or silent cloud
fallback. Ending media must not cancel accepted Agent work.

Regression-check `/phone` start/mute/captions/recovery/end and verify reciprocal
busy behavior. Test mobile permission/background behavior only on actual devices;
mark unavailable environments untested. Keep evidence local/private and redacted.

## Finish and rollback

Fix narrow defects on the feature branches with focused tests. Report actual
passed/failed/untested scenarios, effective endpoints without secrets, paired
SHAs and exact local configuration changes. Prepare a main-integration plan but
wait for merge approval. Rollback disables `/call` and restores prior qualified
Function/client versions; it does not rewrite source history or delete Session
records. Retain `/phone` throughout.
