# HASHI Remote Terminal (Channel B)

## Goal

Execute reliable commands on the target device over adjacent-network remote
access (SSH or other authenticated shell provided by HASHI remote).

## Lab defaults (APT-HW-0001)

| Item | Value |
|---|---|
| Host | `192.168.0.41` |
| User | `apten` |
| Key/known_hosts | project SSH material (not in EXP body) |
| Repo | `C:\AptenraDebug\src\Aptenra` |
| Evidence | `C:\AptenraDebug\evidence\` |
| Logs | `%LOCALAPPDATA%\Aptenra\debug\logs\` |

Generalise host/user/paths for other onsite targets.

## Operating rules

1. Prefer **scp of a `.ps1` then execute** over nested quoted remote one-liners.
2. Set `ErrorActionPreference` and write a log file under evidence when long.
3. Never echo API keys, PINs, or full `.env` values into chat or evidence.
4. Default to non-admin shell; elevate only with explicit need and confirmation.
5. For PowerShell over SSH: expect quoting/encoding pain — use files.

## Standard probes

```text
hostname / whoami
Get-Date
Get-Service AptenraHost (if present)
Get-Process electron (if Start Aptenra)
Get-Content ...\debug-desktop-launcher.jsonl -Tail N
git -C <repo> rev-parse --short HEAD
git -C <repo> status -sb
```

## Start Aptenra from remote (when intentional)

Prefer invoking the same entry as the desktop shortcut:

- `%LOCALAPPDATA%\Aptenra\Launch-Aptenra.ps1`
- or `Desktop\Start Aptenra.lnk` via short `cmd /c start`

Then read launcher jsonl for `launch.ready` / `launch.failed`.

## Do not

- Use MEGA or OneDrive as the code transport for this EXP.
- Leave interactive wizards that need TTY (e.g. Hermes setup) half-run over
  non-interactive SSH; hand those to human desktop session.
