# HASHI Unreleased Integration Checkpoint — 2026-08-27

Status: **verified integration checkpoint authorised for ordinary `main` push**

This checkpoint records the current HASHI v4 integration line before its
source publication. It does not create a release tag, certify a production
rollout, or imply that a running instance has adopted the outgoing commit.

## Repository identity

| Field | Value |
| --- | --- |
| Approved GitHub destination | `https://github.com/Bazza1982/HASHI.git` |
| Publication branch | `main` |
| Fetched remote base at audit start | `2b8923bf341dcad4224f62eede20bf6de1be99a1` |
| Pre-documentation implementation head | `3c5e806de85ef1b6453466db5d502a1a48b931fc` |
| Divergence at audit start | `0` behind, `16` commits ahead of `origin/main`, plus the reviewed outgoing working tree |
| Push shape | ordinary fast-forward; no force push or history rewrite |

The outbound range begins after the fetched remote base and ends at the `main`
tip containing this checkpoint. The source push is intentionally separate from
runtime reboot, deployment, release tagging, and live-provider certification.

## Integrated scope

### HER v2 continuity and provider routing

- HER v2 keeps unfinished observable work in a crash-safe, Agent-local WIP
  Journal. Error and interrupted turns preserve it; a later durable
  `COMPLETED` Ledger clears it atomically.
- WIP lifecycle evidence is written independently as content-free
  start/inject/preserve/clear events in the HER v2 audit log, with a durable
  Agent-local fallback when the primary log is unavailable.
- Journal context is neutral background. It does not auto-resume an old task or
  override the current request.
- OpenRouter and DeepSeek remain concrete HER v2 providers but are no longer
  selectable top-level `/backend` engines. A legacy direct selection migrates
  only when the Agent already authorises an explicit HER v2 row.
- Scheduled HER v2 jobs use Direct (`zero`) execution instead of inheriting an
  interactive high-effort plan.

### Multimodal routing

- `deepseek-v4-flash-vision-exp` is registered as the exact DeepSeek model with
  native image capability.
- Other DeepSeek models remain text-only unless separately proven and
  registered; image capability is never inferred from provider name alone.
- Media retains stable order and identity through the provider-neutral native
  or authorised-local-fallback contract.

### Persistent client-neutral sessions

- Session API v1 supplies durable Session, Message, Run, Event, attachment,
  approval, consumer-ACK, fencing, context-generation, and promotion services.
- Restart reconciliation interrupts orphaned queued/running Runs, advances the
  fencing generation, preserves Messages, and emits durable interruption
  evidence.
- Activation remains fail-closed behind its qualification boundary.

### Telegram notification control

- `/notify` now supports `on`, `quiet`, and `off` as persisted per-Agent
  workspace preferences.
- Quiet delivers all messages, silences interim acknowledgements, commentary,
  reasoning, technical activity, placeholders, and previews, and uses normal
  notification signalling for completed results, errors, warnings, recovery,
  control messages, and important alerts.
- For a split final response, only the last chunk notifies in Quiet mode.
- Notification-policy errors cannot suppress message delivery. Hot reload
  loads the notification foundation before its consumers and validates the
  current three-state interface before accepting the refreshed runtime.

## Documentation reconciliation

The active documentation now:

- documents Quiet notification semantics, persistence, failure safety, and the
  Telegram sound/vibration boundary;
- documents WIP Journal lifecycle files, audit events, sensitive-state rules,
  and correct operational interpretation;
- describes Fixed as a mode inside the sole supported Flex Agent runtime,
  rather than as the removed second runtime implementation;
- removes active references to deleted legacy runtime files and the obsolete
  `conversation_log.jsonl` split;
- marks the Florence-2 preprocessing design and the 12 August runtime
  modularisation plan as historical rather than current guidance;
- updates the provider-only OpenRouter/DeepSeek, DeepSeek native vision,
  Session API, `/long` media, release-note, roadmap, and checkpoint guidance;
  and
- retains older release and implementation documents only when their historical
  scope is explicit.

## Verification evidence

All commands below ran from the repository root. The complete offline suite
used the project virtual environment (`Python 3.10.12`) so optional encryption
dependencies came from the declared environment rather than an incomplete
host-global Python installation.

| Gate | Result |
| --- | --- |
| Complete offline product suite | `2981 passed`, `5 skipped`, `5 deselected` using `python -m pytest -q tests -m "not live and not platform and not real_wall_clock"` |
| Focused notification, reload, WIP, Session, configuration, and multimodal suite | `314 passed` |
| Deterministic core gate | `266 passed` |
| DeepSeek provider-directory and canonical-audit regression pair | `2 passed` |
| Foreground cancellation cleanup on Python 3.10 and 3.12 | `1 passed` on each interpreter; cleanup truth verified through the durable audit record |
| Python compilation for every changed Python module and test | passed |
| Ruff for every changed Python module and test | passed |
| Git whitespace/error check | passed |
| Internal Markdown target check | `320` Markdown files scanned; `227` repository targets checked; `0` missing |

The complete offline run excludes tests explicitly marked `live`, `platform`,
or `real_wall_clock`. It does not claim a real provider call, mobile Telegram
notification receipt, or operating-system-specific cold-start validation.

## Known boundaries

- Quiet controls Telegram's Boolean silent-message flag. Telegram and the
  phone decide sound and vibration; HASHI cannot request those independently.
- A WIP injection proves recovery context was supplied, not that the model
  correctly inferred or resumed the interrupted task.
- Persistent Session API activation remains fail-closed until its complete
  qualification matrix passes for the intended deployment.
- Cross-provider real multi-image canaries remain required before declaring
  broad provider/model production coverage.
- The legacy no-CLI onboarding fallback can validate an OpenRouter key, but
  OpenRouter is provider-only; a usable startup still requires an explicit HER
  v2 configuration.
- Source publication does not reboot Agents, modify host-local launcher files,
  create a release tag, or certify a production deployment.

## Publication decision

The reviewed implementation and documentation are suitable for three ordinary
commits: one for Quiet notification/runtime behavior, one for cross-version and
DeepSeek catalogue test alignment, and one for the documentation
reconciliation. The authorised publication is an ordinary push of local `main`
to GitHub `main`; no force push or tag is part of this checkpoint.
