# HASHI Live Runtime Protection

Status: implementation contract

Owner: PAO with Platform Configuration

Engineering layers: Functions tool admission, Platform Configuration, and
Instance Configuration. Protected Core source remains immutable.

## Purpose

Protect the running HASHI Core and its interpreter without making an Agent's
workspace, workzone, development checkout, or independent development
environment read-only.

This boundary is deliberately exact:

- protected Core source paths come only from
  `orchestrator.runtime_contract.CORE_SOURCE_PATHS` beneath the configured
  `project_root`;
- the live interpreter root is the Function Worker's actual `sys.prefix`;
- the configured instance secrets file is unreadable through Agent file and
  shell tools;
- the current Core PID cannot be terminated with the generic process tool;
- an optional instance-local policy may add exact service configuration,
  restart-secret, runtime-root, and service-name targets;
- all other paths remain governed by the existing workzone and Tool Registry
  permissions.

The Tool Registry evaluates this gate only when a Tool is requested. It does
not scan the repository and is not part of ordinary message or model routing.

## Agent-visible behavior

Agent tools reject:

- `file_write` or `apply_patch` against an authoritative Core path, the live
  interpreter, the policy file, or another exact configured live target;
- `file_read`, `log_query`, or shell access to the configured secrets and
  restart-key paths;
- `pip` or `uv` package mutation that implicitly uses the live interpreter, or
  explicitly names that interpreter;
- generic `process_kill` against the current Core PID;
- raw service-control commands for the current HASHI instance or another exact
  configured HASHI service target.

The gate allows:

- ordinary writes in an authorized workspace or workzone;
- an explicitly selected independent development Python environment;
- read-only Core inspection such as `git diff`, `git show`, `Get-Content`,
  `cat`, `stat`, or hashing;
- control of an unrelated service, subject to existing Tool permissions;
- the supported HASHI `/restart` command and `/reboot` lifecycle.

A denial is audited with `reason=live_runtime_protection`, the operation, and
the exact target. It tells the Agent to use a development environment or the
supported lifecycle command.

## Instance-local extension

The optional file is local state and must not be committed:

```text
<bridge_home>/state/platform/live-runtime-protection.json
```

Schema 1 accepts only absolute paths:

```json
{
  "schema": 1,
  "runtime_roots": [],
  "protected_write_paths": [],
  "protected_read_paths": [],
  "service_targets": []
}
```

The file can only extend the derived baseline; it cannot disable Core,
interpreter, secret, or PID protection. Unknown fields, relative paths, or a
wrong schema invalidate the extension as a unit. The derived baseline remains
active so a malformed optional file neither removes Core protection nor makes
the Agent's whole workzone read-only.

The policy itself is a protected write target. Platform deployment must apply
native ACLs to it together with the live targets.

## Deployment command

The platform helper is `scripts/protect_live_runtime.py`. All roots are
required explicitly; it refuses filesystem roots, repository/instance roots as
protection targets, missing Core files, and runtime roots that are neither a
virtual environment nor the exact `sys.prefix` reported by their own Python
interpreter. This includes standalone Python installations managed by `uv`
without accepting an arbitrary directory as a runtime.

Always start with the read-only plan:

```text
python scripts/protect_live_runtime.py plan \
  --instance-id HASHIX \
  --code-root <live-code-root> \
  --bridge-home <instance-home> \
  --runtime-root <live-python-environment> \
  --secrets-path <instance-secrets>
```

`plan` creates no directory or policy file. `apply` additionally requires the
exact confirmation `PROTECT LIVE RUNTIME HASHIX`; `restore` requires
`RESTORE LIVE RUNTIME HASHIX`. Service definitions, restart secrets, and exact
service names are added with repeatable `--service-config`,
`--restart-secret`, and `--service-target` arguments.

On POSIX, apply/restore runs as root and names the non-root runtime user and
group. Targets become root-owned and read/execute-only for that group; the
top-level target also receives the immutable bit. On Windows, the runtime SID
receives read/execute, while SYSTEM and Administrators retain full control.
`--lock-owner` also moves ownership to Administrators and therefore requires an
elevated deployment process.

The Windows non-elevated mode blocks real file modification and child creation,
but the same file owner can deliberately replace its ACL. It is a canary and
compatibility mode, not account isolation. Strong Windows deployment needs an
Administrator-owned dedicated live parent; a live environment placed beneath
an Agent-writable repository can still have its top-level directory renamed by
that owner. The Tool Registry blocks supported Agent routes in both layouts,
but reports must retain this distinction.

`verify` performs a real runtime-identity write-open/create probe and requires
read access plus write denial for every exact target. Rollout retains the
previous immutable source/environment as the authoritative rollback; the
helper's `restore` operation only removes the applied canary restriction so
that trusted recovery can proceed.

## Windows service-backed restart

A Windows instance installed as a service must list that exact service name in
`service_targets`. Hashi Remote never infers the service from a folder name or
accepts an arbitrary service argument. When the configured service name exactly
matches the controlled instance ID, `/restart` uses
`bin/hashi_service_ctl.ps1`; a development instance without that explicit
target keeps the existing non-service launcher.

The platform helper `scripts/protect_windows_restart_service.py` grants the
Remote runtime principal only `SERVICE_START` and `SERVICE_STOP` for that one
configured service. Start with `plan`; `apply` requires the exact confirmation
`GRANT RESTART SERVICE HASHIX`, and `restore` requires
`REVOKE RESTART SERVICE HASHIX`. Applying the service ACL is an elevated
deployment action. It does not grant configuration, deletion, generic command,
or access to any other service.

Agent tools remain unable to issue raw service-control commands. An authorized
Agent may request the supported `/restart` operation through Remote. A returned
launcher PID or service `Running` state is not success: the terminal receipt
must prove that the old Core PID exited, a different Core PID is healthy, the
instance identity matches, and the runtime version and Function generation are
the expected values.

The service-control PowerShell child uses the Windows no-window launch mode,
not `DETACHED_PROCESS`; detached mode can return a launcher PID without
executing service control. Terminal verification allows normal service
stop/start and product readiness to complete before it records failure.

## Operating-system boundary

Tool admission is an early explanation, not a substitute for operating-system
permissions. Deployment must give the normal runtime identity read/execute
access to Core source and the live interpreter while reserving mutation for a
trusted deployment identity. Service definitions and restart secrets receive
the narrow permissions appropriate to their consumers.

When Core and Agent Workers share one operating-system identity, an ACL cannot
distinguish their code by intent. In that topology:

1. native ACLs still make live source and the interpreter read-only to the
   normal unelevated identity;
2. Tool Registry admission blocks supported Agent Tool routes;
3. trusted deployment remains a distinct elevated/root operation;
4. the instance must not be described as cross-account isolated.

Changing every Worker to a new operating-system account is not part of this
fix. It can break interactive credentials and approved Engine Providers. Any
future identity split needs a separately tested platform migration.

## Verification

Focused verification proves:

- Core and live-interpreter writes are denied;
- live dependency installation is denied;
- secrets are not returned by Tool routes;
- the current Core PID and exact HASHI service cannot be controlled through
  generic tools;
- workspace writes and explicit development environments still work;
- malformed optional policy does not widen or globally freeze access;
- Windows and WSL native permissions deny real write attempts in a disposable
  target before any instance adoption.

Live adoption follows the approved rollout: disposable target first, then only
the explicitly authorized HASHI2 and HASHI3 canaries. HASHI1 and HASHI4 are not
restarted for this work.
