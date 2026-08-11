# Aptenra HP (APT-HW-0001) Comprehensive Testing Report

**Report date:** 2026-07-26 (AEST)
**Test window:** 2026-07-25 ~14:00 – 2026-07-26 ~00:05 AEST
**Target:** APT-HW-0001 · `192.168.0.41` · user `apten`
**Operator plane:** Mother / WSL + Windows PiKVM tools
**EXP framework:** `/exp aptenra debug` (playbooks 00–09)
**Product scope:** Aptenra Debug / Developer Runtime (source-mode); Aptenra-only (HASHI core not modified by these tests)

---

## 1. Executive summary

| Item | Result |
|---|---|
| **Overall lab ship gate (smoke)** | **PASS** |
| **Blocking bugs found in final retests** | **0** |
| **Blocking bugs found earlier and fixed** | **1** (UTF-8 BOM offline config) |
| **Dual-channel (KVM + SSH) achieved** | **YES** (late session) |
| **Offline local model (Gemma 4 E4B)** | **PASS** (text complete) |
| **Full product UI coverage (playbook 09)** | **PARTIAL** (Day A done; Day B / Round 3 not fully executed) |

**Bottom line:** Debug Runtime on HP is **stable enough for continued lab use** on baseline `feature/hashi-remote-hp@c5a9d7b3` with offline Gemma active and Qwen preserved. Remaining gaps are **product-surface depth** (Workbench fully interactive UI, shell browser, online chat UI, voice cache), not pipeline integrity.

---

## 2. Environment under test

| Item | Value |
|---|---|
| Host name | APT-HW-0001 |
| Hardware | Ryzen 7 7730U · ~15GB RAM · Radeon iGPU (prior notes) |
| LAN | `192.168.0.41:22` |
| SSH | `apten` + `%LocalAppData%\Aptenra\credentials\debug-sync\A9_MAX_to_APT-HW-0001_ed25519` |
| PiKVM | `10.0.0.3` · user `admin` · password via `pikvm_web_admin.dpapi` |
| Runtime class | `developer_source` / Debug Runtime |
| Final source branch | `feature/hashi-remote-hp` |
| Final source commit | `c5a9d7b3759e93bc7f992d5c3f3e1dee15417ddb` (`c5a9d7b3`) |
| Ledger | `%LOCALAPPDATA%\Aptenra\debug\current.json` matched HEAD after reloads |
| Active offline model | `gemma-4-e4b-it-dev` → `...\gemma-4-E4B-it-Q4_K_M.gguf` |
| Preserved offline profile | `profiles\qwen2.5-7b-instruct-dev.json` |
| Llama toolchain | `C:\AptenraDebug\toolchain\llama.cpp\b10069\llama-server.exe` |
| Desktop | `Start Aptenra Developer.lnk`, `Start Workbench.lnk`, companion pet; no `Electron.lnk` |

### Related code commits exercised / produced

| Commit | Role |
|---|---|
| `16b2da7d` | Merge HASHI Remote editable instance name into main |
| `48eb6ddb` | Dev offline ≥4B gate; `thinking_enabled` / `verbose_enabled` knobs |
| `c5a9d7b3` | Offline config load via `utf-8-sig` (BOM tolerance) |

Mother branches aligned at end of merge work: `main`, `feature/hashi-remote-hp`, `feature/hashi-remote-editable-instance` (tips advanced with offline commits).

---

## 3. Test campaigns chronology

| # | Campaign | Mode | Channels | Outcome |
|---|---|---|---|---|
| T1 | Model speed bench (pre-Gemma product config) | Investigate | SSH | Gemma E4B fastest (~5.8 t/s) vs Qwen2.5 / Qwen3 |
| T2 | Offline Gemma product config | Deploy + config | SSH | Configured; Qwen preserved |
| T3 | Chat failure after Gemma config | Bugfix | SSH | **BOM root cause fixed** |
| T4 | Playbook 08 smoke_plus | Formal smoke | B only (`remote_only`) | **Ship gate PASS** |
| T5 | Round-2 retest + model switch | Regression | B | **PASS · bugs=0** |
| T6 | Playbook 09 Day A dual-channel | Enhanced | **A+B (`dual`)** | **PASS core · partial surfaces** |
| T7 | Plans only (Round 3 gap plan) | Plan | — | Not executed as full run |

---

## 4. Campaign detail

### 4.1 T1 — Model comparison (bench only)

| Model | Approx gen (bench) | Notes |
|---|---:|---|
| Gemma 4 E4B Q4 | ~5.8 t/s | Speed winner on this PC |
| Qwen2.5-7B | ~4.1–4.3 t/s | Prior product default |
| Qwen3-8B | ~3.4–3.8 t/s | Same tier, not faster |

Reason/verbose structured probe incomplete historically (quoting + freeze). Product later added prompt knobs, not Activity stream.

### 4.2 T2 — Gemma offline configuration

| Check | Result |
|---|---|
| Active `config.json` → Gemma | Pass |
| Qwen profile file retained | Pass |
| `models_catalog.json` | Pass |
| Dev gate 4.5B parameters | Pass (`48eb6ddb`) |
| think/verbose flags true on Gemma | Pass |

### 4.3 T3 — Critical bug: message not sent

| Field | Detail |
|---|---|
| **Symptom (UI)** | 「这条消息尚未发送。内容已保留…可以重试。」 |
| **Root cause** | PowerShell `Set-Content -Encoding UTF8` wrote **UTF-8 BOM**; Python `json.loads(utf-8)` failed → offline config invalid |
| **Fix (state)** | Strip BOM from all `offline_assist` JSON on HP |
| **Fix (code)** | `c5a9d7b3` load with `utf-8-sig` |
| **Verify** | `LOAD_OK gemma-4-e4b-it-dev 4.5 ready []`; chat path unblocked |

### 4.4 T4 — Playbook 08 smoke_plus

**Evidence:** `C:\AptenraDebug\evidence\hp-human-debug-20260725-235034\`

| Phase | Result | Notes |
|---|---|---|
| P0 Preflight | PASS | HEAD=ledger `c5a9d7b3` |
| P1 Cold start | PASS | ~2.0 s process up; second start ~832 ms; HASHI TCP 18939 |
| P2 Brand | PASS | `Aptenra.exe`; Developer .lnk; no Electron.lnk |
| P3 Online chat | SKIP | remote_only |
| P4 Offline | PASS | Cold complete ~9.9 s; warm ~5.1 s; llama log shows Gemma |
| P5 Profiles | PASS | Files present (full desktop switch later in T5) |
| P6 Instance name | PASS | validate rejects empty/spaces/65 chars |
| P7 Resilience | SKIP | Timebox |
| P8 Regressions R1–R8 | PASS | R5 only historical “already running” in stderr |
| **Ship gate** | **PASS** | P0+P1+P2+P4+P8 R1–R4 |

### 4.5 T5 — Round-2 retest (switch + core)

**Evidence:** `C:\AptenraDebug\evidence\hp-retest-20260725-2355\`

| Check | Result |
|---|---|
| Gemma complete `GEMMA_OK` | PASS ~5.5 s warm |
| Switch → Qwen `QWEN_OK` | PASS ~8.1 s · no BOM |
| Switch → Gemma restore | PASS ~35 s cold reload |
| Last llama load path | Gemma gguf |
| Host severity=error (last 2000) | 0 hits |
| New blocking bugs | 0 |

**Non-blocking:** `voice.backend.stderr` — HF `local_files_only` missing cached snapshot when offline.

### 4.6 T6 — Dual-channel Day A (playbook 09 subset)

**Evidence (KVM):**
`%LocalAppData%\Aptenra\debug-sync\evidence\APT-HW-0001\`
- `p0a-desktop-latest.jpg` — desktop, companion, shortcuts
- `p1-after-aptenra-clicks.jpg` — Workbench splash **74%**
- HID: `/api/hid/events/send_mouse_move|send_mouse_button`

| Check | Result |
|---|---|
| PiKVM auth | PASS (DPAPI; prior 401 fixed) |
| HID double-click | PASS |
| Companion visible | PASS |
| Start Aptenra tooltip (Debug Runtime) | PASS |
| Workbench splash | PASS (“Connecting your AI workspace…”) |
| Workbench electron/node processes | PASS |
| Offline Gemma `FULL_OK` | PASS (~55 s after runtime stop) |
| Browser UI drive-through | NOT DONE |
| Online primary chat UI | NOT DONE |
| Canvas deep | NOT DONE |
| Voice cache fix | NOT DONE |

---

## 5. Metrics (lab HP)

| Metric | Observed |
|---|---|
| Cold desktop Aptenra process up | ~2 s (ledger aligned) |
| Second start | ~0.8 s |
| Offline warm short complete | ~5–9 s typical |
| Offline cold after model switch | ~35 s (Gemma reload) |
| Offline after explicit runtime stop | ~55 s |
| Bench gen tokens/s (earlier) | Gemma ~5.8 · Qwen2.5 ~4.2 · Qwen3 ~3.6 |

---

## 6. Issues register

| ID | Severity | Status | Description | Resolution |
|---|---|---|---|---|
| HP-OFF-001 | **P0** | **Fixed** | UTF-8 BOM in offline `config.json` → messages not sent | Strip BOM + `utf-8-sig` (`c5a9d7b3`) |
| HP-SSH-001 | P1 | Mitigated | Wrong SSH user/key → auth denied | Use `apten` + debug-sync key |
| HP-SSH-002 | P1 | Documented | SSH-spawned Aptenra children die with OpenSSH job | schtasks / desktop durable start |
| HP-LEDGER-001 | P1 | Mitigated | Desktop revision mismatch after branch deploy | Always Reload after commit switch |
| HP-VOICE-001 | P2 | Open | Voice backend fails offline without HF cache | Pre-cache models or improve UX; text OK |
| HP-WB-001 | P3 | Open (partial) | Workbench confirmed splash/processes; full interactive UI not fully certified | Round 3 R3-1 |
| HP-BRW-001 | P3 | Open | Embedded browser not UI-tested end-to-end | Round 3 R3-4 |
| HP-CHAT-UI-001 | P3 | Open | Online primary chat not UI-tested on HP this campaign | Round 3 R3-2 |

---

## 7. Artefacts & documentation produced

| Path | Purpose |
|---|---|
| `exp/.../playbooks/08_comprehensive_hp_human_debug_test.exp.md` | Smoke / ship gate plan |
| `exp/.../playbooks/09_enhanced_hp_full_coverage_test.exp.md` | Full blind-spot plan |
| `exp/.../EXP.md` + `manifest.json` | Registered 08/09 |
| `C:\AptenraDebug\evidence\hp-human-debug-20260725-235034\` | Smoke pack |
| `C:\AptenraDebug\evidence\hp-retest-20260725-2355\` | Round-2 pack |
| `...\debug-sync\evidence\APT-HW-0001\*.jpg` | KVM snapshots |
| This report | Comprehensive write-up |

---

## 8. Coverage matrix (what was / was not tested)

| Area | Status |
|---|---|
| Git/ledger/BOM/regressions | **Done** |
| Desktop start / brand / companion visual | **Done** |
| Offline text (API + config) | **Done** |
| Model profile switch | **Done** |
| HASHI listen + instance validation | **Done** |
| KVM auth + HID | **Done** |
| Workbench launch splash | **Done** |
| Workbench main interactive UI | **Partial / open** |
| Online primary chat UI | **Open** |
| Offline chat via full shell UI | **Partial** (API strong; UI after BOM fix assumed, not re-screenshot every turn) |
| Embedded browser navigation | **Open** |
| Stop button UI | **Open** (runtime stop only) |
| Canvas | **Open** |
| Voice end-to-end | **Open** (failure mode noted) |
| Specialists UI | **Open** (packages present) |
| Telegram/WhatsApp live | **Open** |
| OpenRouter connect UI | **Open** |
| Crash/reboot resilience | **Open** |
| Device git pull-back loop | **Open** |

---

## 9. Recommended next test (Round 3) — not yet run

Prioritised **60–90 min** gap closure (do **not** re-run full BOM/Gemma API smoke unless code changes):

1. Workbench → interactive main UI snapshot
2. Online chat one turn (if WAN)
3. Offline chat one turn in shell UI
4. Embedded browser one HTTPS page
5. Stop generation from UI
6. Optional: Canvas / voice failure copy / settings glance

Gate: `PASS` iff items 1–5 pass (or 2 explicitly skipped with WAN down).

---

## 10. Conclusions

1. **Lab Debug Runtime on HP is operational** at `c5a9d7b3` with dual-channel observability restored.
2. The only **P0 product bug** introduced during Gemma config (**BOM**) was **found, fixed, and re-verified**.
3. Offline **Gemma** is a valid speed-oriented Dev experiment; **Qwen** remains recoverable via profile copy (no BOM).
4. **Ship gate for smoke (08) is green.**
5. **Full coverage (09) is not complete**; highest residual risk is **Workbench/browser/chat UI depth** and **voice offline assets**, not core Host/offline load path.
6. Operational rules confirmed for future work: durable start via desktop/schtasks; Reload after branch switches; never `Set-Content -Encoding UTF8` for offline JSON without no-BOM encoding.

---

## 11. Sign-off

| Role | Statement |
|---|---|
| Test executor | Campaigns T1–T6 executed as documented above |
| Ship gate (smoke) | **PASS** as of 2026-07-25 night retests |
| Full product certification | **Not claimed** — Round 3 still recommended |

**Report id:** `HP_COMPREHENSIVE_TEST_REPORT_20260725`
**Author:** Aptenra dual-channel lab session (玉环 / operator agent)
