# HASHI3 local development migration, 2026-10-06

The user authorized migrating current HASHI1/HASHI2 development and test work
into a new local HASHI3 development branch, preserving Simple, `/call` and
minor fixes. The baseline is the actual HASHI4 production `main`, rather than
the older `main` pointers in the development instances. Core remains identical
to that baseline. This is source consolidation, not runtime adoption.

## Owners and included development

| Owner | Preserved development |
| --- | --- |
| Frontend Connector | Simple navigation, session scope and drafts, native browser support; cloud-classified call input, current-recording fences, early speech synthesis, diagnostic journal and camera context |
| PAO | Creation template selection through the existing typed manager, explicit model restrictions, restricted intent, Move consistency fences, Remote and platform repairs |
| PCM | Role-preserving Phone handoffs, current-Run Session attachment consumption, released Codex voice transcripts and nightly context corrections |
| HERV3 | Scoped search and visible tool/process activity, persistent Run question contracts and current call conversation policy |

The frontend is an independent repository on the same named local development
branch. Its accepted Simple/call tree is the previously combined ordinary-page
publication, not an older call-only experiment. Simple was already published
through the shared frontend; its presence here does not imply that production
HASHI4 lacks the existing UI. Run questions have a backend contract; a complete
external-frontend question-answer flow remains unverified.

## Integration choices

- Preserve production's single model/effort resolver, explicit empty model
  restrictions, typed PAO state writer and nonblocking Telegram startup/retry.
  Add diagnostics and template intent through those existing boundaries.
- Port exact-file attachment grants to native Windows with pinned read
  handles, rejected reparse points, frozen identities and byte integrity checks.
  Do not grant an attachment's directory to tools or CLI backends.
- Preserve every original local branch and all source stashes as local
  migration/archive refs, plus byte-exact uncommitted-file and patch snapshots.
  Old Rika, avatar and desktop drafts that are already incorporated or replaced
  by later implementations remain recoverable; do not restore retired writers,
  unrestricted environment loaders or superseded UI layouts.
- Keep instance configuration, identity, credentials and all source checkouts
  separate from the new source branch. Platform opt-ins are proposals to be
  reconciled before adoption, not permissions copied from another instance.

## Qualification and delivery boundary

Focused validation covers call, Simple/drafts, scoped search/activity,
questions, creation/persistence, Telegram ingress and platform behavior. The
new Windows parent-write fence and template model restrictions have recorded
red/green scenarios. Protected Core checks and whitespace checks apply to the
complete branch delta. Independent review covers preservation and the merged
ownership/security seams; it is not an exhaustive audit of the whole library.

Function generation/process qualification requires a committed source
checkpoint; final Core gate, isolated Worker and WSL receipts are recorded with
the local migration evidence. They do not prove running Workers have adopted
the branch. Physical noise/short-speech/latency acceptance and native frontend
interaction remain distinct, future acceptance work.
