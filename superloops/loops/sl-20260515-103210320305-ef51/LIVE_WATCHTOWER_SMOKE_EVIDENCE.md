# Live Watchtower Smoke Evidence

Date: 2026-05-15
Orchestrator: Zelda
Target: HASHI9 Windows-native Remote, isolated smoke root

## Purpose

This evidence closes `sli-121728730437-abde`: Zelda had not personally verified that Watchtower can actually start through the live rescue API before superloop exit.

The test used real Windows Python and the real Remote API implementation from `C:\Users\thene\projects\HASHI`, but a temporary HASHI root so the real HASHI core was not restarted.

## Isolated Roots

- L3 root: `C:\Users\thene\projects\HASHI\tmp\watchtower_smoke`
- L2 root: `C:\Users\thene\projects\HASHI\tmp\watchtower_smoke_l2`

The L3 root contained a dummy fixed launcher:

```powershell
param([string]$Action, [switch]$Resume)
Write-Output "watchtower smoke bridge_ctl action=$Action resume=$Resume"
Start-Sleep -Milliseconds 200
exit 0
```

## Remote Startup

L3 Remote was started with Windows Python:

```text
C:\Users\thene\projects\HASHI\.venv\Scripts\python.exe -m remote.main --host 127.0.0.1 --port 35991 --no-tls --hashi-root C:\Users\thene\projects\HASHI\tmp\watchtower_smoke --max-terminal-level L3_RESTART --supervised --verbose
```

Observed startup:

- instance: `WATCHTOWER_SMOKE`
- platform: `windows`
- server: `http://127.0.0.1:35991`
- capabilities included `rescue_control` and `rescue_start`

L2 Remote was started on `127.0.0.1:35992` with `--max-terminal-level L2_WRITE`.

## L3 Smoke Results

| Check | Result |
|---|---|
| `GET /control/hashi/status` | `200`, `state=offline`, `hashi_running=false`, `pid_file_exists=false`, `pid_alive=false` |
| `GET /control/hashi/logs?name=start&tail=5000` before start | `200`, `effective_tail=1000`, `tail_truncated=true`, `exists=false` |
| `GET /control/hashi/logs?name=../../secret` | `400`, invalid log name rejected |
| `POST /control/hashi/start` with long multiline reason | `200`, `started=true`, `launcher_kind=powershell.exe`, `platform=windows` |
| start command | `powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Users\thene\projects\HASHI\tmp\watchtower_smoke\bin\bridge_ctl.ps1 -Action start -Resume` |
| reason handling | CR/LF collapsed, persisted reason truncated, `reason_truncated=true` |
| `GET /control/hashi/logs?name=audit&tail=20` | `200`, audit record exists |

Audit record contained:

- `requester=lan-client`
- `outcome=started`
- `pid=28456`
- `command=[powershell.exe, ..., bridge_ctl.ps1, -Action, start, -Resume]`
- `log_path=C:\Users\thene\projects\HASHI\tmp\watchtower_smoke\logs\remote_rescue_hashi_start.log`
- `reason_original_length=748`
- `reason_truncated=true`
- `status_state=offline`

## L2 Gate Smoke Results

| Check | Result |
|---|---|
| `GET /protocol/status` on L2 Remote | `200`, capabilities include `rescue_control` but not `rescue_start`; `rescue_start_enabled=false` |
| `POST /control/hashi/start` on L2 Remote | `403`, `HASHI start requires max_terminal_level=L3_RESTART` |

## Cleanup

Temporary Windows Remote processes were stopped after the smoke test.

Follow-up note: Windows console emitted `UnicodeEncodeError` logging errors for decorative banner glyphs under CP1252, but the Remote server still started and the Watchtower rescue API worked. This is a nonblocking console logging issue outside the Watchtower v1 API contract.
