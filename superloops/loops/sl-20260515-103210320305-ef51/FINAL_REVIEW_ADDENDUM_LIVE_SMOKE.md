# Final Review Addendum: Zelda Live Watchtower Smoke

Reviewer requested: Akane
Date: 2026-05-15
Loop: `sl-20260515-103210320305-ef51`
Issue closed: `sli-121728730437-abde`

## Why This Addendum Exists

After the final review, the user correctly challenged the exit because Zelda had not personally run a live Watchtower start verification. Worker-reported smoke evidence was not enough for an automatic superloop exit.

The loop was reopened and the missing high-severity issue was tracked as:

`sli-121728730437-abde` — Orchestrator-side live Watchtower start verification missing before exit.

## New Evidence Artifact

Full evidence is recorded in:

`superloops/loops/sl-20260515-103210320305-ef51/LIVE_WATCHTOWER_SMOKE_EVIDENCE.md`

## What Zelda Tested

Zelda started real Windows-native Remote processes using:

- implementation root: `C:\Users\thene\projects\HASHI`
- Windows Python: `C:\Users\thene\projects\HASHI\.venv\Scripts\python.exe`
- module: `remote.main`
- isolated L3 smoke root: `C:\Users\thene\projects\HASHI\tmp\watchtower_smoke`
- isolated L2 smoke root: `C:\Users\thene\projects\HASHI\tmp\watchtower_smoke_l2`

The temporary roots prevented accidental restart of the real HASHI9 core while still exercising the real `/control/hashi/*` API and Windows launcher selection logic.

## Results

| Requirement | Result |
|---|---|
| Windows-native Remote starts | Passed: `WATCHTOWER_SMOKE` served `http://127.0.0.1:35991` |
| L3 advertises rescue start | Passed: capabilities included `rescue_control` and `rescue_start` |
| Status endpoint works when HASHI core is down | Passed: `state=offline`, `hashi_running=false` |
| Logs endpoint bounds `tail` | Passed: `tail=5000` produced `effective_tail=1000`, `tail_truncated=true` |
| Logs endpoint rejects invalid name | Passed: `name=../../secret` returned `400` |
| Start endpoint executes fixed Windows launcher | Passed: `started=true`, `launcher_kind=powershell.exe`, `platform=windows` |
| Start command is not arbitrary shell | Passed: command was fixed `powershell.exe ... bridge_ctl.ps1 -Action start -Resume` |
| Reason sanitization/truncation works | Passed: multiline long reason collapsed and `reason_truncated=true` |
| Audit record exists for started path | Passed: audit JSONL includes requester, outcome, command, pid, log_path, reason fields |
| L2 rejects start | Passed: `POST /control/hashi/start` on L2 returned `403` |
| L2 does not advertise rescue_start | Passed: capabilities had `rescue_control` but not `rescue_start`; `rescue_start_enabled=false` |
| Temporary Remote cleanup | Passed: ports `35991` and `35992` were down after cleanup |

## Nonblocking Observation

Windows console emitted `UnicodeEncodeError` for decorative banner glyphs under CP1252 during Remote startup. The server still started and all Watchtower API checks passed. This appears to be a console logging/banner portability issue, not a Watchtower v1 API blocker.

## Request To Reviewer

Please review this addendum only:

1. Does the new orchestrator-run live smoke evidence satisfy the previously missing exit criterion?
2. Does the CP1252 banner logging issue block Watchtower v1 exit, or should it be tracked as a later LOW issue?
3. Are there any remaining blockers before Zelda exits `audit_vibe_coding`?
