# HASHI2 /call experiment status — 2026-10-04

Owner: Frontend Connector Functions and Workbench UI. PAO SessionStore remains
the canonical conversation record. The experiment has been hot-adopted only
by HASHI2, with its paired isolated Workbench. It is not merged to `main`.
Physical microphone, speaker, camera, and `/phone` acceptance await the user.

## What is ready offline

- The qualified `/call` source is integrated with the current HASHI2 and
  Workbench development baselines in isolated worktrees. Existing `/phone`
  Realtime transport and its settings remain independent.
- The initially disabled OpenRouter profile selects `openai/whisper-large-v3` for STT and
  `google/gemini-3.8-flash-lite-tts` for speech. It lists the Gemini voices and
  their descriptors, plus a separate bounded speaking-style option. The style
  is sent as Google AI Studio speech metadata, never spoken as text. Gemini
  requires PCM output; the Function packages its validated raw PCM as WAV for
  the existing browser player.
- The backend resolves its OpenRouter key through the existing instance secret
  resolver. Workbench receives only the safe catalogue. The front-end flag
  hides the `/call` entry and card as well as blocking new proxy operations.
- OpenRouter may route Whisper Large V3 to several providers and its speech API
  currently ignores provider-routing preferences. A response is labelled Groq
  only when a generation receipt verifies that actual provider; otherwise the
  provider remains unverified. The model name alone cannot prove Groq service.
- Transcriptions and replies use PAO's existing SessionStore. The earlier
  handover's `conversation_log.jsonl` filename does not describe the current
  HASHI2 authoritative store; no second memory or call database is added.

## Evidence and remaining gates

- Focused HASHI2 Python tests: 92 passed. Workbench focused Node tests: 52
  passed; its production build passed. Protected Core checker passed. These are
  offline results, not physical or paid-provider acceptance.
- At first the ignored HASHI2 key returned HTTP 401. The user authorized a
  narrow copy from HASHI1; its `openrouter_key` passed authentication and is now
  the HASHI2 `openrouter-api_key`. Other HASHI2 secret fields and the original
  root ownership, mode, and immutable protection were verified unchanged.
- The first authenticated Gemini request failed because `mp3` is unsupported
  for this model. A corrected raw PCM request returned HTTP 200 with a non-silent
  24 kHz mono stream. The corrected Function adapter then returned a valid
  1.64-second WAV for voice `Sulafat` and a separate speaking style.
- That WAV was resampled to the normal 16 kHz call input. OpenRouter Whisper
  transcribed the expected Chinese phrase as `你好,测试`. This verifies the real
  TTS-to-STT provider path, not browser playback or PAO turn projection.
- OpenRouter returned generation IDs for both speech calls but its generation
  lookup returned 404, including after the calls had finished. Actual serving
  providers therefore remain unverified. The user accepts a non-Groq provider;
  no Groq-specific claim is needed for this experiment.
- The corrected source and Core gate passed, and runtime adoption plus PAO
  projection have now been verified below. Real microphone, loudspeaker,
  camera, voice/style preference, and `/phone` acceptance remain open.
- Adoption of the shared Backend API and Agent Functions requires an expressly
  approved HASHI2 operational scope and a fresh idle/queue/schedule check.
  The specific hot `/reboot max` trial was approved later on 2026-10-04; cold
  HASHI2 restart remains outside scope. Code and test success cannot be
  described as live adoption.
- Accept with the paired Workbench branch on real microphone,
  loudspeaker and camera, with a short `/phone` regression and a measured
  rollback. Only after HASHI2 live acceptance should any other instance be
  considered.

## Initial live adoption preflight (historical)

- The user explicitly approved the previously proposed HASHI2-only hot
  `/reboot max` trial and its necessary rollback. This does not approve a Core
  change, a cold restart, another instance, or a merge to `main`.
- Before changing the active HASHI2 source, the live Backend API showed five
  active Agents, none generating or holding queued requests, and no managed
  background jobs. All five authoritative Agent Scheduler lists had no enabled
  jobs.
- Three nightly QA Agents still reported active cross-instance transfers to
  HASHI3. The target recorded one transfer as `received` only, one as
  `accepted_but_chat_offline`, and one as `accepted`. The `received` transfer
  had no queued target request. Adoption was held while source and target
  execution evidence were investigated; transfer flags alone did not prove
  that work was still running.
- At that checkpoint HASHI2 retained its original root branch and running
  Function generation. The later investigation and adoption below supersede
  that operational blocker; physical-device acceptance is still pending.

## Approved runtime adoption and API acceptance — 13:14 AEDT

### Handoff disposition and safety window

- The user approved continuing the handoff investigation and HASHI2-only
  experiment adoption at 12:32 AEDT. No live transfer state was changed.
- The received-only record is a historical unknown-outcome QA guard probe.
  Its source transcript records completion without work or replay; its target
  has no admitted request. The unknown-outcome fence remains held, not marked
  accepted or completed. Both accepted transfers have completed target PAO
  Runs. No source or target Run remained active for these three handoffs.
- Fresh checks immediately before each hot replacement confirmed all five
  active HASHI2 Agents idle with empty queues, no managed background work,
  no active PAO Runs or calls, and no task due within 30 minutes. Active Agent
  Scheduler lists and the configured inactive-Agent schedules were checked.
  Old Scheduler recovery batches were neither replayed nor resolved.
- All three source transfer-fence files were byte-identical after replacement.
  The operational maintenance blocker is closed through execution evidence
  and preservation; the historical unknown-outcome guard is retained.
- The earlier one-shot nudge `lin_yueru-nudge-5328ec3d` actually ran once and
  is authoritatively disabled with `exit_condition_met`; no duplicate exists.

### Source and running processes

- Active HASHI2 source: `experiment/call-hashi2-live-20261004`, commit
  `b35f5fb40587049798bbe8205a7df5ead0711401`. The reviewed worktree remains
  separate on `experiment/call-hashi2-20261004`; subsequent documentation-only
  commits do not change the running source generation.
- Native Workbench source: isolated `experiment/call-hashi2-20261004`,
  commit `06b9e74`, served at `http://127.0.0.1:3012/`, restricted to HASHI2.
  Its owned preview uses a verified portable Node 22 runtime; other Workbench
  processes and development changes were left intact.
- The real Workbench hot control displayed its progress banner and obtained
  a browser presentation acknowledgement. Operations
  `fb537833f5214c389c2e12059f6b30ed` and
  `fa82a68813a94be9851022f83b33e1d6` both succeeded and committed.
- Backend API and all five Agent Workers adopted qualified Function generation
  `sha256:6dacc7b72a7cc40ef50d7cafc83676f7ebef322627c289e7e25351a2bb347919`.
  Shared Function PID is `371320`; Core PID remains `838` and its digest is
  unchanged. No Core source, Core cold restart, or `main` merge was involved.
- The first live `/call` context exposed a missing standard
  `workbench_admin_token`. It was added only to ignored HASHI2 secrets through
  the revisioned configuration writer, retaining other fields, ownership,
  permissions, and immutable protection. The second approved hot replacement
  loaded it through the existing Remote-to-Backend credential mechanism;
  no authentication code or roles were changed. Context then returned 200.
- The ignored HASHI2 OpenRouter call profile is now enabled. Its default voice
  is `Sulafat`, with a separate warm, concise Mandarin speaking style and
  Gemini vision target. Existing `/phone` configuration is independent.

### Real service chain, browser view, and PAO

- A generated audio sample and a synthetic image exercised the normal paired
  Workbench proxy: context, start with cloud consent, turn, snapshot, speech,
  and end. No physical microphone, speaker, or camera was used.
- OpenRouter Whisper transcribed `你好,测试`; Gemini vision successfully
  described the synthetic blue rectangle. The actual Arale Agent completed
  its reply, and Gemini TTS returned a valid 7.48-second WAV first segment.
  Turn-to-answer latency was 14,278 ms for this one sample, not a measurement
  of physical device or playback performance.
- PAO Run `run_f5c11ae60c5c4a7b95f8284a73025db0` completed in Arale's canonical
  primary Session. Its idempotency key has exactly one Run and exactly one
  user message plus one assistant message. No second conversation writer was
  introduced. The canary call was ended and fresh context reports not busy.
- A real headless browser rendered the Workbench call settings: 30 Gemini
  voices, default `Sulafat`, separate speaking style, and configured vision
  choices, with no page errors. This verifies rendering, not device quality.
- `/phone` context returned 200 and capability available; its existing
  `gpt-live-1` / `Marin` settings were preserved. Physical `/phone` regression
  has not yet been accepted.
- Actual speech-serving provider identity remains unverified as described
  above; the user accepts a non-Groq provider.

### Evidence, next owner, and recovery

- Private HASHI2 evidence is in `state/call-hashi2-20261004/`, including
  `final_adoption_and_pao_verified.json`, preflight snapshots, preserved
  transfer snapshots, and private configuration recovery metadata. The native
  Workbench's ignored `state/call-hashi2-20261004/` holds UI receipts,
  `live_api_canary.json`, audio, screenshots, and the owned launch receipt.
  Credentials and conversation payloads remain outside Git.
- Next owner is lin_yueru: invite the user to open the 3012 entry on this PC,
  choose Arale, and test `/call` microphone transcription, voice and style,
  speaker, camera snapshot, then end the call and regress `/phone`. Record the
  user's observations separately from the API and screenshot evidence.
  Investigate feedback immediately; the existing maintenance heartbeat is
  the fallback. Do not add a duplicate wake-up or promote other instances.
- Necessary rollback remains scoped to HASHI2: end active test calls, recheck
  idle/queue/background/schedule safety, disable the call profile through its
  revisioned writer, and restore baseline source `9abae11` through an approved
  hot replacement if needed. Stop only the owned 3012 preview. If the new
  service credential must be reverted, remove only that added field using the
  recorded revisioned recovery procedure and restore original file protection;
  retain the working OpenRouter key, Session history, and transfer fences.
  Never use a Core cold restart as rollback.

Delivery state: implementation and focused tests passed; isolated runtime
adoption and live API/PAO acceptance passed; `main` merge and other-instance
promotion not performed; physical device and user acceptance remain open.

## User entry failure and recovery — 2026-10-04

- The user's browser screenshot showed connection refused at the 3012 entry.
  The original preview PID and foreground tool session were both absent.
  HASHI2 Backend API and the qualified experiment Function generation remained
  ready. The exact preview exit reason was not captured; the previous check
  had established readiness without verifying launcher lifetime.
- Only the owned isolated Workbench was recovered. Its hidden launch now
  uses the external Windows WMI process broker, rather than the foreground
  execution session. Preview PID `34720` is outside the calling tool's Windows
  job, and remained ready after the launching shell exited. No instance
  replacement, new Scheduler task, or product-code change was needed.
- HTTP entry and real browser call settings/context returned 200 with the
  intended HASHI2 connection, 30 voices and default Sulafat, and no page errors.
  A new screenshot capture timed out after those checks; it is not counted as
  new visual evidence. The earlier screenshot and the user's failure image
  remain separate from the recovery's process, DOM and API evidence.
- Recovery receipt: `state/call-hashi2-20261004/preview_recovery_20261004.json`.
  User acceptance is still open. Owner lin_yueru invites the user to refresh
  the same entry and resume physical `/call` and `/phone` acceptance. Feedback
  triggers immediate follow-up; the existing maintenance heartbeat is fallback.

## User call result and menu review — 2026-10-04

- At 13:58 AEDT, the user reported that the test succeeded. Record this as
  user-reported `/call` success; it is not a separate confirmation of every
  device, mobile behavior or physical `/phone` regression. The experiment
  remains open for those specific acceptance gaps and the reported UI feedback.
- The reported missing `/call` picker entry was reproduced and fixed in the
  paired isolated Workbench branch, commit `bb095d2258832b1062400354246d134ce43ace7b`.
  The call feature owns its command parser and metadata; the existing local
  catalogue projection retains server descriptions and denials. Selection
  inserts text only, and disabled/unavailable states retain their boundaries.
  Focused red/green and related command/call checks passed (35), and the
  production frontend bundle built successfully.
- Only the preview's static frontend bundle was updated. Old hashed assets
  were retained and the index was replaced atomically; preview PID `34720`
  remained running. HASHI2 source, Core and Functions were not replaced in
  this UI follow-up. Private Workbench receipts are
  `state/call-hashi2-20261004/picker_bundle_adoption.json` and
  `state/call-hashi2-20261004/picker_ui_verified.json`.
  A fresh real headless browser loaded the new bundle, searched and selected
  `/call`, and verified `/call ` insertion with no call card, media acquisition,
  call mutation or page errors. User desktop and physical devices were untouched.
- The menu layout is a reviewed proposal, not an adopted UI change. It keeps
  actual calls in the floating card and moves preparation/configuration into
  ordinary command menus with common navigation. Desktop and mobile share
  menu content and persistence rules; live persistent configuration stays
  read-only until the call ends. See the paired Workbench's
  `docs/call/CALL_MENU_REVIEW_2026-10-04.md` and `LOCAL_HANDOVER.md` FYI.
- Next owner is lin_yueru: report the fixed picker and concrete menu proposal;
  implement the layout according to the user's feedback, retaining the existing
  profile writer, provider facts and call lifecycle. Menu/device observations
  trigger immediate follow-up; existing maintenance heartbeat is fallback.
  Do not repeat dispatches, add a wake-up, merge main or promote other instances.

## Approved menu redesign and camera entry — 15:01 AEDT

- The user's 14:28 approval covers the ordinary-menu / floating-call-card split.
  The screenshot shows active `/phone` at right and unstarted `/call` at left;
  it does not establish physical `/call` camera acceptance.
- Frontend Connector / Workbench UI source is committed as
  `a80aa01c5f07c6b4950a316fa415bd28ddf673aa` on the isolated experiment.
  `/call` opens an ordinary inline menu with Sound, Picture and Advanced pages.
  Navigation uses shared command-menu controls; the existing Profile API remains
  the only persistent writer. No synthetic server menu identities or PAO
  messages are created.
- Voice and camera start are explicit, with cloud consent and a one-image-per-
  speaking-turn explanation. Active settings are read-only and keep the same
  media session. Failed saves retain drafts and are not automatically retried.
  Existing user-selected voice/style values were read, not overwritten.
- 78 focused checks and the production build passed. Focused red/green evidence
  covers old pre-call floating settings, active field locks, ordinary-menu
  failure blocking and confirmed save results.
- Only the existing 3012 preview's static frontend artifacts were adopted.
  The index was atomically replaced, old assets retained and preview PID 34720
  preserved. Actual headless checks loaded the new bundle, verified inline
  navigation, 375px compact layout, refresh and close, with only context reads
  and no media acquisition or persistent writes.
- A separate, fully intercepted headless call used synthetic camera/microphone
  devices and mock call responses. Camera start produced a live 1024x576
  preview; active settings, minimize/return and exactly one start passed.
  No start/end reached the real Backend API. No page errors occurred.
  This is camera UI evidence, not physical device or Agent observation acceptance.
- Private paired Workbench receipts are `menu_bundle_adoption.json` and
  `menu_ui_verified.json`. Recovery copies `menu_predeploy_index.html` and
  `menu_atomic_prior_index.html` retain the previous index; both generations'
  hashed assets remain available.
- Fresh HASHI2 health confirms Core PID 838, unchanged protected source digest,
  and the same qualified shared/Agent Function generation; shared PID 406741.
  Active HASHI2 source remains clean at b35f5fb4. No Core/Function replacement,
  main merge or other-instance adoption occurred in this UI follow-up.

Delivery state: menu implementation, focused checks and experimental frontend
adoption passed. Physical camera observation, per-device/mobile user acceptance
and complete `/phone` regression remain open. Owner lin_yueru invites the user
to end the current `/phone`, refresh the same 3012 page, select Arale, run
`/call`, consent to selected cloud media, choose camera start and allow browser
microphone/camera permissions. Ask the Agent to describe visible clothing or a
gesture to confirm real observation. Follow user feedback immediately; existing
maintenance heartbeat is fallback without duplicate dispatch or new wake-up.
