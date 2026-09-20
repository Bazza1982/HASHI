# Windows deployment assets

This directory owns Windows host deployment for a source checkout. It is a
platform adapter, not HASHI Core.

## WSL user runtime at interactive logon

Use `install-wsl-hashi-user-runtime.ps1` from an elevated Windows PowerShell
5.1 prompt to register one explicit instance. For example:

```powershell
.\packaging\windows\install-wsl-hashi-user-runtime.ps1 `
    -InstanceId research `
    -Distro Ubuntu-22.04 `
    -LinuxRoot /home/me/projects/hashi `
    -LinuxPython /home/me/projects/hashi/.venv/bin/python3 `
    -StartNow
```

The installer:

- validates the named distribution, checkout, and interpreter before writing;
- copies the versioned launcher to the shared ProgramData deployment area;
- creates a Highest, interactive-logon task for the exact Windows identity;
- permits battery operation, ignores duplicate starts, and has no execution
  time limit; and
- replaces the shared launcher transactionally, retaining the prior file as
  `start-wsl-hashi-user-runtime.ps1.previous`.

The registered action passes the instance, identity, distribution, checkout,
and interpreter explicitly. It does not infer identity from a folder name and
does not share an instance data directory.

The launcher writes three independent logs beneath the instance's ProgramData
`logs` directory:

- `user-runtime.log` contains launcher lifecycle records;
- `user-runtime.stdout.log` contains WSL standard output; and
- `user-runtime.stderr.log` contains WSL standard error.

Windows PowerShell 5.1 may project native standard error as
`NativeCommandError`. Standard error is diagnostic data, not a process result;
the launcher therefore redirects the native streams directly and propagates
the actual `wsl.exe` exit code. Stream logs are archived between launches so
they are never appended into the UTF-8 lifecycle log or mixed with a previous
encoding. A legacy lifecycle log containing NUL bytes is also archived on the
first upgraded launch, even when it has not reached the normal size limit.

Stop the exact scheduled task/runtime before replacing its deployment, then
use `-StartNow` or `Start-ScheduledTask` and verify the instance's own health
and identity. Installing source bytes, registering the task, and observing a
healthy running generation are separate facts.

## Native Windows source-checkout runtime at interactive logon

Use `install-native-hashi-user-runtime.ps1` from an elevated Windows
PowerShell 5.1 prompt for a native Windows source checkout. For example:

```powershell
.\packaging\windows\install-native-hashi-user-runtime.ps1 `
    -InstanceId research `
    -HashiRoot C:\src\hashi-research `
    -ApiGateway `
    -StartNow
```

The installer validates the checkout's configured `instance_id`, interpreter,
entrypoint, Windows identity, and exact task before changing deployment state.
It installs a parameterized shared launcher transactionally and registers a
Highest interactive-logon task with no runtime or idle-end cutoff, bounded
restart-on-failure, battery operation, missed-start recovery, and duplicate
start suppression. Machine identity, checkout paths, and instance names are
task arguments rather than tracked template literals.

As with the WSL launcher, native stdout and stderr go directly to independent
files. They never pass through a Windows PowerShell pipeline: PowerShell 5.1
turns native stderr merged by `*>>` into a terminating `NativeCommandError`
when `$ErrorActionPreference` is `Stop`, which can kill the Core process tree.
The native child's real exit code is the only process-result authority. Legacy
mixed-encoding lifecycle logs and prior stream logs are archived before launch.

## Native Windows portable runtime

The native portable installer is independently owned by
`packaging/portable_windows`. Its launcher uses the packaged interpreter,
quoted arguments, separate stdout/stderr files, and a verified local endpoint.
Do not route a native portable installation through either source-checkout
login task.

The native Windows Remote controller in `bin/hashi_remote_ctl.ps1` registers a
separate Limited task with an unlimited execution lifetime, missed-start
recovery, duplicate-start suppression, and bounded restart intervals. It is
not the Highest WSL Core login task.
