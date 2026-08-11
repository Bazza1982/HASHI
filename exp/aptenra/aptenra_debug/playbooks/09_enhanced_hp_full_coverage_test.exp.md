# Enhanced HP Full-Coverage Debug Test Plan

**EXP:** `aptenra/aptenra_debug`
**Playbook id:** `09_enhanced_hp_full_coverage_test`
**Parent:** `08_comprehensive_hp_human_debug_test` (smoke / ship gate)
**Target (lab):** APT-HW-0001 · `192.168.0.41` · `apten`
**Operator:** mother / HASHI control plane
**Mode:** dual-channel preferred (`dual`); document `remote_only` limits

## Intent

Playbook **08** proved cold start, brand, offline text (Gemma/Qwen), ledger/BOM traps, and HASHI Remote instance validation.

This **enhanced** plan closes **function blind spots**: surfaces that exist in product code but were skipped, only partially probed, or never exercised as a human would.

**Rule remains:** human journey first → KVM sees → Remote proves → evidence without secrets.

---

## Coverage delta (08 → 09)

| Area | 08 status | 09 requirement |
|---|---|---|
| Preflight / start / brand | Covered | Keep (P0–P2) |
| Online primary chat | Skip-prone | **Mandatory if WAN** (P3+) |
| Offline text complete API | Covered via Python | **Also UI chat path** under airplane mode |
| Activity sidebar (cloud) | Mentioned | **Human assert stream rows** |
| Activity sidebar (offline) | Known gap | **Document actual UX** (static vs empty) |
| Think/verbose offline | Config only | **UI body shows reasoning sections** |
| Voice / ASR / TTS | Not tested | **P10 Voice** |
| Live transcription / translation | Not tested | **P11 Specialist tools** |
| Pass a note | Not tested | **P11** |
| Embedded browser + vault/PIN | Not tested | **P12 Browser** |
| Workbench full path | Shortcut only | **P13 Workbench** |
| Canvas / artefacts | Not tested | **P14 Workbench canvas** |
| WhatsApp / Telegram remote | Not tested | **P15 Channels** (creds optional) |
| OpenRouter account connect | Not tested | **P16 Accounts** |
| AI runtime routing UI | Partial | **P17 Routing / agents** |
| Proactive / agent activity | Not tested | **P18 Proactive** |
| Desktop companion pet | Log only | **P19 Companion visual** |
| Language / i18n switch | Not tested | **P20 Locale** |
| Enrolment / capacity display | Health only | **P21 Enrolment & capacity** |
| Privacy / data export hints | Not tested | **P22 Privacy** |
| Crash / sleep / reboot | Soft skip | **P7 hard** |
| PiKVM dual-channel | Often skip | **P0A mandatory for dual claim** |
| Git pull-back HP→mother | Not tested | **P23 Device commit loop** |
| Multi-window / multi-monitor | Not tested | **P24 Shell UX edge** |
| Long chat / history scroll | Not tested | **P25 Chat history** |
| Stop / cancel generation | Not tested | **P26 Cancel turn** |
| File attach / paste image | Not tested | **P27 Composer media** |
| Settings IA full walk | Partial Debug | **P28 Settings walkthrough** |
| Observability control panel | Not tested | **P29 Observability** |
| Hermes coexistence | Installed separately | **P30 Side-by-side tools** |

---

## Execution modes

| Mode | Duration | Phases |
|---|---|---|
| **Smoke (08)** | 35–45 min | P0–P2, P4, P8 critical |
| **Ship gate** | ~60 min | Smoke + P3 (if WAN) + P6 + P10 smoke |
| **Full coverage (09)** | **4–8 hours** (human-paced, may split days) | All phases below |
| **Night batch** | Unattended Remote subsets | API offline, git, ledger, profiles; **no** HID claims |

Split full coverage into **Day A / Day B** if needed:

- **Day A — Core product:** P0–P9 (08) + P10–P13 + P17 + P19 + P25–P26
- **Day B — Integrations:** P11, P12 deep, P14–P16, P18, P20–P24, P27–P30

---

## Baseline block (fill every full run)

```text
RUN_ID:
DATE:
OPERATOR:
SESSION_STATE: dual|remote_only|...
CHANNEL_PLAN: A+B|B|A
BASELINE_COMMIT:
BRANCH:
ACTIVE_OFFLINE_MODEL:
WAN: up|down
VOICE_MODELS_CACHED: yes|no
CHANNEL_CREDS_AVAILABLE: none|telegram|whatsapp|both
```

---

## Phase catalogue

### P0–P9 — inherit from playbook 08

Run unchanged. Record evidence under the same run folder.

**Enhancement to P3:** do not skip merely because operator is remote — if WAN is up, open shell via durable start and drive chat through **Host IPC** *and* (if dual) KVM visual confirmation of bubbles.

**Enhancement to P4:** require **UI** offline path (airplane mode + send in shell), not only `LlamaCppRuntime.complete`.

**Enhancement to P5:** after profile copy, **desktop restart** (not only Python `rt.stop`), then UI message.

---

### P0A · Dual-channel visual (blind spot: Channel A)

| Step | Human / KVM | Remote |
|---|---|---|
| Auth PiKVM | Login Web UI (creds from project store only) | — |
| Snapshot | `p0a-desktop.png` | — |
| Classify | desktop / login / no_signal | — |
| HID short | Win+D, open Start menu | Confirm no long paste |

**Pass:** authenticated snapshot stored; video class recorded.
**Fail:** 401 / no_signal → cannot claim dual; continue `remote_only`.

---

### P10 · Voice path (blind spot)

#### Human
1. Open voice / mic controls in shell (settings + composer mic).
2. With **WAN up:** push-to-talk or dictation if product enables.
3. With **WAN down / airplane:** retry; note error UX (not silent hang).
4. Playback TTS if offered after reply.

#### Remote proof
- [ ] `electron-shell.jsonl` for `voice.backend.stderr` / success events.
- [ ] HF / model cache presence under user profile (path recorded, no secrets).
- [ ] `local_files_only` failure if cache missing → **known defect if UX is opaque**.

#### Pass criteria
| Sub | Pass |
|---|---|
| Online voice | User hears/sees transcription or clear “not configured” |
| Offline voice | Clear message; does not break text chat |
| Cache missing | Actionable UI or settings deep-link |

#### Fix guidance if fail
- Pre-cache required snapshot on HP while online **or** ship local-only voice assets.
- Never leave “message not sent” for pure voice-backend issues without copy.

---

### P11 · Specialist tools (blind spot)

Tools under product: **live transcription**, **live translation**, **pass a note**.

| Tool | Human steps | Remote |
|---|---|---|
| Live transcription | Open tool → start → speak 5s → stop → text appears | tool run logs / events |
| Live translation | Source→target lang → sample utterance | same |
| Pass a note | Compose note → send/save → appears in history | store path / no secret leak |

**Pass:** each tool opens without white screen; cancel works; no crash.
**Skip:** if skill not in rolepack for this enrol — mark `SKIP_ROLEPACK` with evidence.

---

### P12 · Embedded browser (blind spot)

#### Human
1. Open in-app browser.
2. Navigate to a simple HTTPS page (e.g. example.com).
3. New tab / close tab / reload.
4. Overflow menu (⋮) not covered by WebContentsView (recent chrome bugs).
5. If PIN vault enabled: set PIN (user types; **never log PIN**), save dummy site credential, autofill prompt.
6. Private tab: vault actions refused.
7. Find-in-page if available.

#### Remote
- [ ] Browser controller process healthy.
- [ ] No uncaught exceptions in shell log during tab ops.
- [ ] Vault files exist only under expected state paths (no password plaintext in logs).

#### Pass
Navigation + multi-tab + menu not occluded; vault only when unlocked.

---

### P13 · Workbench start & identity (blind spot)

#### Human
1. Quit Workbench if running.
2. Double-click **Start Workbench**.
3. Wait for UI (note blank period; progress splash if any).
4. Open one known Workbench view (home / tasks).
5. Second click activates existing window.

#### Remote
- [ ] workbench electron/server/client PIDs or control jsonl.
- [ ] Ports match expected debug workbench.
- [ ] Branding not generic Electron window title if product renames.

#### Pass
Interactive Workbench < 2 min cold (lab laptop); no infinite spinner without error.

---

### P14 · Canvas / artefacts (blind spot)

#### Human
1. In Workbench, create or open **Canvas**.
2. Add text node / intent brief if UI offers.
3. Save / load / export if available.
4. “Give to Agent” / convert package if present (confirm dialog).
5. Cancel mid-flow.

#### Remote
- [ ] Canvas files under expected state dirs.
- [ ] Encryption flags if feature-on: ciphertext not plain secrets in logs.

#### Pass
Save→reload round-trip without data loss for a simple canvas.

---

### P15 · Remote channels Telegram / WhatsApp (blind spot)

#### Human (only if founder supplies test channel)
1. Open remote channel settings.
2. Status badges: not configured vs ready.
3. Save allowed numbers / chat ids (test values).
4. Send inbound test message from phone → appears in Aptenra.
5. Outbound / proactive if enabled.

#### Remote
- [ ] Channel event poll returns sequences.
- [ ] No tokens in evidence JSON.

#### Pass / Skip
- Pass: inbound visible.
- `SKIP_NO_CREDS` if no test account — still check **settings load without crash**.

---

### P16 · OpenRouter / cloud account (blind spot)

#### Human
1. Settings → OpenRouter / account.
2. Disconnected state clear.
3. Connect flow starts (OAuth or key) — **user completes secret entry**.
4. Disconnect / revoke path.

#### Remote
- [ ] `getOpenRouterAccount` ok; no key material in logs (`[REDACTED]`).
- [ ] Status flips connected only after user action.

#### Pass
UI never shows full API key; error paths polite.

---

### P17 · AI runtime routing & agents (blind spot)

#### Human
1. Open AI runtime / agent routing UI.
2. List primary / specialists / offline_assist binding.
3. Switch primary display (e.g. claw vs grok-cli) **if offered** — do not break offline.
4. Revert to known-good routing after test.

#### Remote
- [ ] `ai_runtime` activation document / bindings file changes only expected keys.
- [ ] Offline still works after routing edit.

#### Pass
Routing change is visible and reversible; offline path intact.

---

### P18 · Proactive / agent activity (blind spot)

#### Human
1. Enable a safe proactive/demo action if available.
2. Activity sidebar shows proactive entries.
3. Dismiss / clear works.

#### Remote
- [ ] proactive channel events or activity projection events.

#### Pass
No phantom loops; clear empties UI.

---

### P19 · Desktop companion (blind spot)

#### Human (KVM strongly preferred)
1. Companion visible on desktop after start.
2. Modes: idle → thinking (send chat) → completed.
3. Right-click / menu if any.
4. Does not steal permanent focus from typing.

#### Remote
- [ ] character pack integrity ok (from health/getCharacterPack).
- [ ] renderer state ready in shell log.

#### Pass
Visible pet + mode changes correlate with chat.

---

### P20 · Locale / language (blind spot)

#### Human
1. Switch UI language (e.g. zh-CN → en → back).
2. Chat chrome, settings labels, offline banner i18n.
3. Restart app; language persists.

#### Remote
- [ ] locale file / settings store value.

#### Pass
No mixed mojibake; critical strings present in both langs.

---

### P21 · Enrolment & capacity (blind spot)

#### Human
1. Open about / capacity / enrolment status UI.
2. Confirm enrolled Client Zero mode without re-enrol storm.

#### Remote
- [ ] host health `enrolment.enrolled=true`.
- [ ] capacity remaining fields sane.

#### Pass
No forced re-enrol on normal start.

---

### P22 · Privacy & data surfaces (blind spot)

#### Human
1. Open privacy / data / preview panels.
2. Confirm no accidental “export all secrets”.
3. Clear chat history for one conversation (if offered) — confirm only that thread.

#### Remote
- [ ] cleared thread gone from store; others remain.

---

### P23 · Device Git loop (blind spot)

#### Human / Operator
1. On HP, make a **tiny** doc-only or comment-only change on `feature/hashi-remote-hp`.
2. Commit on device branch policy (`device/APT-HW-0001` or feature branch — follow project rule).
3. Push to bare; mother fetches and reviews.
4. **Do not** push secrets.

#### Pass
Round-trip commit visible on mother; HP worktree clean after.

---

### P24 · Shell UX edges (blind spot)

#### Human
1. Resize activity / chat splitters.
2. Restart mid-resize.
3. Multi-monitor drag if hardware allows.
4. Zoom / DPI 125–150% readability.

#### Pass
No unusable layout; settings persist.

---

### P25 · Chat history (blind spot)

#### Human
1. Send 20 short messages (mix online/offline if possible).
2. Scroll to top; search if available.
3. New conversation; switch back; history intact.
4. Clear one conversation only.

#### Pass
No lost history; no unbounded memory hang.

---

### P26 · Cancel / stop generation (blind spot)

#### Human
1. Offline or online: send long prompt.
2. Hit **Stop** while generating.
3. UI returns to idle; can send again.
4. No zombie llama slot forever.

#### Remote
- [ ] `assistant_turn_cancel_v1` or equivalent event.
- [ ] Subsequent complete succeeds.

#### Pass
Cancel < 3s perceived; next turn works.

---

### P27 · Composer media (blind spot)

#### Human
1. Attach allowed file types (txt/png as product allows).
2. Reject oversized / disallowed type with clear error.
3. Paste image from clipboard if supported.

#### Pass
Accept/reject rules match product; no crash.

---

### P28 · Settings full walkthrough (blind spot)

Walk **every** top-level settings category (Information Architecture):

- [ ] General / profile
- [ ] Voice
- [ ] AI runtime / agents
- [ ] Offline assist status
- [ ] HASHI Remote
- [ ] Remote channels
- [ ] OpenRouter / accounts
- [ ] Browser / PIN
- [ ] Privacy
- [ ] About / diagnostics
- [ ] Language

For each: open → no blank panel → change one non-destructive toggle → revert.

#### Pass
Zero white screens; toggles persist after restart for one sample setting.

---

### P29 · Observability / control panel (blind spot)

#### Human
1. Open observability / diagnostics control panel if present.
2. Trigger a health refresh.
3. Confirm HOST_OK / readable errors.

#### Remote
- [ ] `getHealth` diagnostic_code HOST_OK.
- [ ] runtime_compatibility notes understood (deployment_ready false is OK in debug source).

---

### P30 · Side-by-side tools (Hermes etc.) (blind spot)

#### Human
1. With Aptenra running, open Hermes Status / CLI if installed.
2. Confirm no port war that kills HASHI 18939.
3. Aptenra chat still works.

#### Pass
Coexistence without forced quit.

---

### P31 · Security negatives (new)

| Case | Expect |
|---|---|
| Invalid IPC / oversized message | Reject, no crash |
| Offline config with BOM reintroduced | Still loads (utf-8-sig) **or** clear error |
| Token fields | Never echo full secret in UI logs |
| Browser private tab vault | Denied |

---

### P32 · Performance budgets (lab HP 7730U)

| Action | Soft budget | Hard fail |
|---|---|---|
| Cold desktop start (aligned ledger) | < 15 s interactive | > 120 s no UI |
| Second start | < 2 s | > 30 s hang |
| Offline warm short reply | < 15 s | > 120 s |
| Offline cold (model load) | < 60 s | > 180 s no reply |
| Workbench cold | < 120 s | infinite spinner |

Record numbers every full run.

---

## Master scorecard (09)

| Gate | Required phases |
|---|---|
| **Smoke** | 08 ship gate |
| **Text product** | + P3, P4 UI, P5 desktop restart, P25, P26 |
| **Voice ready** | + P10 pass online or documented offline limit |
| **Workbench ready** | + P13, P14 |
| **Browser ready** | + P12 |
| **Channels ready** | + P15 (or SKIP_NO_CREDS with settings-only pass) |
| **Full dual-channel sign-off** | All non-skipped + P0A snapshots + evidence pack |

Overall:

```text
PASS_SMOKE | PASS_TEXT | PASS_FULL | FAIL_<phase>
```

---

## Evidence layout (enhanced)

```text
C:\AptenraDebug\evidence\hp-full-YYYYMMDD-HHMM\
  session.json
  scorecard.json
  p00-p09\          # 08 artefacts
  p10-voice\
  p11-specialists\
  p12-browser\
  p13-workbench\
  p14-canvas\
  p15-channels\
  p16-accounts\
  p17-routing\
  p18-proactive\
  p19-companion\
  p20-locale\
  ...
  kvm\              # snapshots only if dual
  NOTES.md          # human observations
```

Each phase folder: `result.json` (`pass|fail|skip`, notes, timings) + optional png.

---

## Anti-patterns (still forbidden)

- Long HID PowerShell paste
- Storing PIN / API keys in evidence
- Claiming dual-channel without P0A
- Using MEGA/OneDrive as code sync
- Skipping UI offline test after only API complete

---

## Mapping to product modules (quick index)

| Module path | Phases |
|---|---|
| `offline_assist/` | P4, P5, P26, P32 |
| `hashi_primary/` | P3, P17 |
| `shell/electron/renderer/browser/` | P12 |
| `specialist_tools/` | P11 |
| `ai_runtime/` | P17 |
| `settings/hashi_remote*` | P6 |
| `remote_*` / channels | P15 |
| Workbench tree | P13, P14 |
| `character*` / packs | P19 |
| Voice / transcription | P10, P11 |

---

## Suggested next run (after this document)

1. **Day A** on HP with PiKVM auth (dual).
2. Prioritise blind spots that users hit daily: **P10 voice**, **P12 browser**, **P13 workbench**, **P26 cancel**, **P3 online UI**.
3. File each failure into `failures/failure_memory.jsonl` with phase id.

---

## Relation to playbook 08

| Doc | Use when |
|---|---|
| **08** | Daily smoke / post-deploy gate |
| **09** | Pre-release / weekly full / after large merges (browser, canvas, voice) |

08 remains the merge gate for Debug Runtime.
09 is the **completeness** gate for “we exercised the product, not just the pipes.”
