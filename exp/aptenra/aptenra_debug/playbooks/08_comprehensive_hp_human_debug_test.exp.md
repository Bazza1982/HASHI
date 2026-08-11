# Comprehensive HP Human-Like Debug Test Plan

**EXP:** `aptenra/aptenra_debug`
**Playbook id:** `08_comprehensive_hp_human_debug_test`
**Target (lab):** APT-HW-0001 · `192.168.0.41` · user `apten`
**Operator:** mother / HASHI control plane
**Mode:** dual-channel (A = PiKVM visual/HID, B = SSH remote)
**Style:** real human user journeys first; remote probes only prove what the user saw

## Intent

Find product issues the way a human would: click desktop icons, send chat, switch offline,
open Debug panels, restart, wait through long loads, and retry after failures.

This plan **extends** existing playbooks `00`–`07`. It does not replace them.
Run under `/exp aptenra debug` rules:

1. Preflight adjacency + channel plan first (`00`).
2. KVM sees/clicks; Remote executes/proves (`01`–`03`).
3. No long HID scripts; copy `.ps1` then run (`02`).
4. Evidence pack without secrets (`06`).
5. No customer PIN/password material in logs or evidence.

## Current lab baseline (update each run)

| Item | Expected (fill at run start) |
|---|---|
| Source branch | `feature/hashi-remote-hp` (or `main` if retargeted) |
| Source commit | e.g. `c5a9d7b3` or newer |
| Runtime class | `developer_source` / Debug Runtime |
| Ledger | `%LOCALAPPDATA%\Aptenra\debug\current.json` matches git HEAD |
| Offline active model | `gemma-4-e4b-it-dev` or `qwen2.5-7b-instruct-dev` |
| Offline profiles preserved | `profiles\qwen2.5-7b-instruct-dev.json` present |
| Desktop | `Start Aptenra Developer.lnk` · brand Aptenra.exe · no Electron.lnk |
| SSH | `apten@192.168.0.41` + debug-sync key |
| PiKVM | `10.0.0.3` (auth required for Channel A proof) |

## Session state matrix

| State | Meaning | Allowed claims |
|---|---|---|
| `dual` | SSH + video | Full UI + log proof |
| `remote_only` | SSH only | No visual pass claims |
| `visual_only` | KVM only | Limited; no log proof |
| `pre_login` | At Windows login | KVM primary |
| `adjacency_missing` | No LAN | Stop dual-channel |

**Stop rule:** if adjacency missing, do not invent dual-channel success.

---

## Phase map (run order)

```text
P0 Preflight          → 00, 02, 05 (light)
P1 Cold start UX      → 04 + human timing
P2 Brand / desktop    → human + remote path checks
P3 Chat happy path    → human chat (online if WAN up)
P4 Offline Assist     → force offline / local model
P5 Model switch       → Gemma ↔ Qwen profiles
P6 Debug panels       → HASHI Remote instance name, AI runtime
P7 Resilience         → restart, second click, crash recover
P8 Regression traps   → BOM, ledger mismatch, SSH job death
P9 Evidence + score   → 06 + pass/fail matrix
```

Estimated wall time (human-paced): **90–150 minutes** full run; **35–45 minutes** smoke subset (`P0–P4` + `P8` critical).

---

## P0 · Preflight (mandatory)

### Human / KVM
- [ ] Screen is desktop (not lock screen). If locked, user unlocks (never store PIN).
- [ ] Snapshot baseline labelled `p0-desktop`.

### Remote
- [ ] `ssh apten@192.168.0.41` succeeds (debug-sync key).
- [ ] `hostname` = `APT-HW-0001`.
- [ ] `git -C C:\AptenraDebug\src\Aptenra rev-parse --short HEAD` matches intended commit.
- [ ] `git status -sb` clean **or** dirty items listed and accepted.
- [ ] Read `current.json`: `source_commit`, `source_branch`, `runtime_class`.
- [ ] Processes: note whether Aptenra already running.
- [ ] PiKVM health (if claiming Channel A): API auth + one snapshot.

### Exit criteria
- [ ] Channel plan written: `adjacency=lan|vpn`, `session_state=...`, `channel_plan=A|B|A+B`.
- [ ] Evidence stub path: `C:\AptenraDebug\evidence\hp-human-debug-YYYYMMDD-HHMM\`.

### Failures to record
| Id | Symptom | Next |
|---|---|---|
| P0-SSH | Publickey denied | Fix key/user `apten`; do not use lily/thene |
| P0-REV | HEAD ≠ ledger | Reload-AptenraDevRuntime before UI tests |
| P0-VID | PiKVM 401/no signal | Mark `remote_only` for visual claims |

---

## P1 · Cold start & second click (human)

### Human path
1. Quit Aptenra completely if running (Alt+F4 all shells; confirm no pet/window).
2. Double-click **Start Aptenra Developer**.
3. Watch: splash / wait / first interactive shell.
4. Note wall-clock: click → interactive (target: few seconds if ledger aligned; multi-minute if full reload).
5. Second double-click within 5s: must **activate existing**, not hang ~90s+.

### Remote proof (after UI)
- [ ] Aptenra / electron process count ≥ 1.
- [ ] Host pipe responsive (any cheap IPC, e.g. offline status or hashi remote status).
- [ ] HASHI listen port from `current.json` / health probe responds (HTTP 200 on known path or documented 404-on-root with open port).
- [ ] Launcher log: `launch.ready` and/or `startup.ready` (see `debug-desktop-launcher.jsonl`).

### Pass
No blocking dialog; interactive UI; remote ready signal; second click < 2s feel.

### Known traps
| Id | Symptom | Root cause pattern |
|---|---|---|
| P1-MISMATCH | Desktop start fails revision | Source HEAD ≠ process ledger |
| P1-ACL | 拒绝访问 | ProgramData / profile ACL |
| P1-HANG2 | Second start long hang | Second instance wait; fail-fast missing |
| P1-SSH-DIE | After SSH-only start, processes vanish | OpenSSH job kills children — use schtasks/desktop |

---

## P2 · Brand, shortcuts, no legacy Electron

### Human
- [ ] Taskbar/window title shows Aptenra product branding (not raw Electron).
- [ ] Desktop has **Start Aptenra Developer** (and Workbench if expected).
- [ ] **No** leftover `Electron.lnk` on desktop.
- [ ] Icon looks like Aptenra value brand (visual check only).

### Remote
- [ ] Process image names include `Aptenra.exe` (not only generic electron for main shell).
- [ ] Desktop shortcut targets `Launch-Aptenra-Developer.ps1` / approved VBS wrapper.
- [ ] Optional: icon hash matches last deploy evidence if available.

### Pass
Brand-consistent shell + correct shortcuts + no Electron.lnk.

---

## P3 · Online chat happy path (if WAN available)

### Human
1. Open main chat with 小能 / primary agent.
2. Send short message: `你好，请用一句话介绍你自己`.
3. Wait for reply (cloud primary if online).
4. Open **活动** side panel: expect activity rows for cloud stream if primary streams.
5. Send second message: confirm session continuity.

### Remote
- [ ] `hashi-primary.jsonl` or host events show request completed without hard error.
- [ ] No stuck `running` turn > 3 minutes without output/error.

### Pass
Two-turn conversation works; activity not empty if product promises stream.

### If WAN down
Mark P3 as `SKIPPED_WAN` and rely on P4.

---

## P4 · Offline Assist (human critical)

### Human setup
1. Prefer real offline: airplane mode or unplug Ethernet **or** force local routing if UI exposes it.
2. Send: `hi` then `用中文说你好`.
3. Observe:
   - Does message send or show “尚未发送” banner?
   - First reply latency (cold llama start may be 10–40s).
   - Banner `Aptenra Offline Assist [local]` in body (product rule).
   - Activity panel: today may only show static “generating” — note actual behaviour.
4. Third longer prompt (~50 tokens expected): time generation feel.

### Remote proof
- [ ] `offline_assist\config.json` loads: `enabled=true`, readiness empty.
- [ ] Config encoding: **no UTF-8 BOM** (or code uses `utf-8-sig`).
- [ ] Active `model_id` / `model_path` exist on disk.
- [ ] New turn file under `offline_assist\turns\` with `state=completed` and `response_source=llama.cpp`.
- [ ] `llama-server` log shows **current** model path (Gemma vs Qwen), not stale model.
- [ ] Timings from turn JSON: `created_at` → `updated_at`.

### Pass
Messages send; local replies complete; config valid; model file matches active id.

### Failure matrix (must hunt)

| Id | Human symptom | Probe | Likely fix |
|---|---|---|---|
| P4-BOM | 消息尚未发送 | JSONDecodeError BOM | Strip BOM; utf-8-sig load |
| P4-GATE | Never local | readiness / param floor | Dev ≥4B; release ≥7B |
| P4-MISSING | Never local | model file missing | Restore gguf path |
| P4-STALE | Wrong speed/quality | llama log still old model | Restart host after model switch |
| P4-SLOW | Usable but slow | ~3–6 t/s on 7730U | Expected; optional threads/warm |
| P4-NOACT | Activity empty | stream:false local | Known gap; document only |

---

## P5 · Model profiles (Gemma ↔ Qwen)

### Human
1. Confirm offline works on **active** model (P4).
2. Operator switches profile (Remote script preferred — see below).
3. Restart Aptenra (desktop) so llama reloads.
4. Repeat short offline chat.
5. Switch back to previous model; retest once.

### Remote switch (safe pattern)
```powershell
# Example: activate Qwen profile (preserve both files)
$root = "$env:LOCALAPPDATA\Aptenra\debug\state\host\offline_assist"
# Write JSON with UTF8Encoding($false) — never Set-Content -Encoding UTF8 (BOM)
Copy-Item -Force "$root\profiles\qwen2.5-7b-instruct-dev.json" "$root\config.json"
# Then durable restart: schtasks Aptenra-Debug-StartNow or desktop shortcut
```
- [ ] `models_catalog.json` lists both profiles.
- [ ] After switch, llama log model path matches.

### Pass
Both profiles produce completed local turns; no BOM regression after switch.

---

## P6 · Debug panels & HASHI Remote instance name

### Human
1. Open **设置 / Debug** (as shipped on current branch).
2. HASHI Remote section:
   - [ ] Instance name field visible.
   - [ ] Invalid name (spaces / empty) shows validation error.
   - [ ] Valid distinct LAN name saves; status refreshes.
3. Optional: AI runtime / offline status panel shows configured model id.
4. Do **not** paste production secrets into chat or evidence.

### Remote
- [ ] Host method `hashi_remote_set_instance_id` rejects invalid id.
- [ ] `agents.json` instance_id/display_name updated after valid save (path per deploy).
- [ ] Remote process restarts only when expected.

### Pass
Editable instance name UX works; invalid rejected; valid persists.

---

## P7 · Resilience & lifecycle

### Human sequences
1. **Kill while generating:** send offline prompt; force-quit app mid-generation; restart; new chat works.
2. **Sleep/wake (optional):** lid sleep 2 min; wake; chat still works or clean recover.
3. **Full reboot (optional heavy):** after boot, desktop Start once; ledger still matches HEAD.
4. **Workbench:** Start Workbench shortcut; wait for UI; no infinite blank (note splash/progress gap if any).

### Remote
- [ ] After crash, no orphaned locked host that blocks relaunch without cleanup.
- [ ] Durable start path preferred after reboot: desktop or `schtasks`, not bare SSH spawn.

### Pass
Recover without reinstall; user can reach chat again.

---

## P8 · Regression traps (from recent lab failures)

Run these even on smoke days.

| # | Trap | Human check | Remote proof |
|---|---|---|---|
| R1 | Ledger ≠ source | Desktop start fails | `current.json` commit vs git HEAD |
| R2 | BOM config | Chat “尚未发送” offline | `python` load config / file head bytes EF BB BF |
| R3 | SSH-spawned death | Works in SSH then dies | Use schtasks/desktop; processes persist after SSH exit |
| R4 | Stale llama model | Config says Gemma, behaviour like Qwen | llama-server.stderr load path |
| R5 | Multi-host instance | Second Host fails | host.stderr “already running” |
| R6 | Dirty tree deploy | Surprises after merge | `git status` before Reload |
| R7 | Brand regression | Electron.lnk returns | Desktop listing |
| R8 | Pre-push remote name | Push blocked | use remote name `hp-test` only |

---

## P9 · Evidence pack & scoring

### Evidence root
`C:\AptenraDebug\evidence\hp-human-debug-YYYYMMDD-HHMM\`

### Minimum pack
- [ ] `session.json` — adjacency, channel plan, baseline commit, operator id (no secrets).
- [ ] `p0-p8-results.json` — each case `pass|fail|skip` + notes.
- [ ] Snapshots (if Channel A): `p1-start.png`, `p4-offline-reply.png`, `p6-debug.png`.
- [ ] Remote excerpts (redacted): last offline turn JSON, config public fields, git HEAD, launcher ready lines.
- [ ] Failure memory append for any new pattern → `failures/failure_memory.jsonl` (lab).

### Scorecard

| Phase | Weight | Pass rule |
|---|---:|---|
| P0 Preflight | 10 | Required |
| P1 Start | 15 | Required |
| P2 Brand | 5 | Required for ship gate |
| P3 Online chat | 10 | Skip if no WAN |
| P4 Offline | 20 | Required for offline claim |
| P5 Model switch | 10 | Required if multi-model lab |
| P6 Debug Remote | 10 | Required if feature on branch |
| P7 Resilience | 10 | Soft for smoke; hard for release |
| P8 Regressions | 10 | All critical traps pass |

**Ship gate (lab Debug):** P0+P1+P2+P4+P8(R1–R4) all pass.
**Full dual-channel sign-off:** all non-skipped phases pass + evidence pack.

---

## Operator command cheatsheet (Remote)

```text
# Identity + git
hostname
git -C C:\AptenraDebug\src\Aptenra rev-parse --abbrev-ref HEAD
git -C C:\AptenraDebug\src\Aptenra rev-parse --short HEAD
git -C C:\AptenraDebug\src\Aptenra status -sb

# Ledger
type %LOCALAPPDATA%\Aptenra\debug\current.json

# Offline config (public fields only)
type %LOCALAPPDATA%\Aptenra\debug\state\host\offline_assist\config.json

# Durable start (survives SSH)
schtasks /Run /TN Aptenra-Debug-StartNow

# Authoritative reload after commit switch
powershell -NoProfile -ExecutionPolicy Bypass -File C:\AptenraDebug\src\Aptenra\scripts\Reload-AptenraDevRuntime.ps1

# Mother push (from mother only)
git -c core.sshCommand=<aptenra-hp-git-ssh> push hp-test main feature/hashi-remote-hp
```

SSH identity (mother WSL):
- User: `apten@192.168.0.41`
- Key: `%LocalAppData%\Aptenra\credentials\debug-sync\A9_MAX_to_APT-HW-0001_ed25519`
- Stage mode `600` when reading from Windows mount.

---

## Smoke subset (daily / after every deploy)

1. P0 preflight
2. P1 cold start + second click
3. P2 brand glance
4. P4 one offline message (or P3 if online-only day)
5. P8 R1 ledger + R2 BOM check
6. Mini evidence JSON

Timebox: **~30–40 min**.

---

## Full suite (pre-merge / pre-HP handoff)

Run P0→P9 complete. Attach scorecard. File new failures into failure memory.
Do **not** mark EXP `stable` until two consecutive full dual-channel runs pass with founder review.

---

## Out of scope (explicit)

- Customer PIN capture or storage
- Public-internet rescue without adjacency
- MEGA/OneDrive as source of truth
- Long HID-typed PowerShell
- Destructive disk wipe without founder approval
- Changing HASHI core / `orchestrator/` unless separately authorised

---

## Mapping to existing playbooks

| This phase | Calls |
|---|---|
| P0 | `00`, `02`, `01` (if visual) |
| P1–P2 | `04` |
| Code sync before test | `05` |
| Loop on any fail | `03` |
| Close-out | `06`, `07` |

---

## Run log template (copy into evidence)

```text
RUN_ID:
DATE:
OPERATOR:
TARGET: APT-HW-0001
ADJACENCY: lan|vpn|missing
SESSION_STATE: dual|remote_only|...
CHANNEL_PLAN: A+B|...
BASELINE_COMMIT:
ACTIVE_OFFLINE_MODEL:
SMOKE|FULL:

P0: pass|fail  notes:
P1: pass|fail  cold_start_s=  second_click_ms=
P2: pass|fail
P3: pass|fail|skip
P4: pass|fail  first_reply_s=  model=
P5: pass|fail|skip
P6: pass|fail|skip
P7: pass|fail|skip
P8: R1..R8:
OVERALL: pass|fail
EVIDENCE_PATH:
```
