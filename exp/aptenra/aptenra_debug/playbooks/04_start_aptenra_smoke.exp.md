# Start Aptenra Smoke (Lab + Onsite UI)

## Goal

Verify the desktop Start Aptenra path works after install or sync.

## Checklist

| Check | KVM | Remote |
|---|---|---|
| Shortcut exists | Icon visible | `Test-Path` Desktop `Start Aptenra.lnk` |
| Launcher present | — | `Launch-Aptenra.ps1` exists |
| No access-denied dialog | Screenshot | No new `launch.failed` ACL errors |
| Shell/companion up | Pet or shell visible | `electron` process count ≥ 1 |
| Ready signal | UI interactive | `launch.ready` or companion `startup.ready` |
| Cold start latency | Subjective | started → ready target ~ few seconds |

## Known healthy patterns (lab)

- Service Host under ProgramData may return `needs_help` while still usable for shell.
- Prefer documented launcher host-selection policy (debug vs service) for the
  commit under test.
- Second click should **activate** existing shell, not hang for long timeouts.

## Known failure patterns

- ProgramData logs/profile not writable → dialog `拒绝访问` / EPERM.
- Second instance + 90s wait after shell exit → fail-fast required.
- HID-launched Run dialog with mangled path → use Remote or short cmd.

## Pass criteria

All of: no blocking error dialog, at least one visual presence signal, and a
matching remote ready/process signal (or documented remote_only exception).
