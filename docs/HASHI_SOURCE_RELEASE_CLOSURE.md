# HASHI source preparation and release adoption

PAO owns source deployment in Functions and platform configuration. Protected
Core, runtime selectors and ordinary Agent tool protection are unchanged.

## Operator commands and result states

| Command | Result | Live actions |
|---|---|---|
| `hashi --instance <name> deploy --confirm <name>` | Ready to reboot | None |
| `hashi --instance <name> update --stage-only --confirm <name>` | Ready to reboot | None |
| `/hashideploy` | Ready to reboot | None |
| `hashi --instance <name> update --confirm <name>` | Complete running release | Core and Remote restart |

CLI source commands default to `origin/main`; `--remote` and `--branch` select
another explicit source. The standard owner-only slash command always prepares
`origin/main` for the current configured instance, with no executable, root,
peer or lifecycle argument. It is discovered by the shared command registry,
including help and frontend command dispatch.

Git source preparation is a supported successful outcome. It explicitly does
**not** mean the target is already running. Complete running adoption still
requires source, Core, shared Functions, every configured Worker and the
independently supervised Remote to report the target commit.

## Preparation without restart

1. Resolve the exact instance and source; CLI requires the exact registered
   name as confirmation. The slash command verifies the authorized owner.
2. Serialize preparation per instance. Require a clean Git checkout and fetch
   the requested branch, allowing only an exact fast-forward. Git hooks are
   disabled so a post-merge hook cannot perform an implicit lifecycle action.
3. Run the protected-Core source gate and invoke the fetched coordinator with
   the separate `stage` verb. An older coordinator without that verb fails
   safely; it cannot fall back to restarting deployment.
4. Verify the installer-owned Core/Functions runtime pair and its file seals,
   then qualify the complete committed Function source/assets, configured
   observers and dependency/import contract in an isolated process. Missing
   or incompatible runtimes produce a useful error; no packages are installed
   in an existing interpreter.
5. Materialize an immutable candidate under a new
   `state/source_deployments/<invocation>` directory. Recheck the exact clean
   target after qualification and publish a private atomic `ready.json`
   receipt. Do not publish an active generation or recovery-cache pointer.
6. Return `state=ready_to_reboot`, `message=Ready to reboot`,
   `restarts_performed=false` and `running_verified=false`, with the exact
   commit, instance, qualification and receipt location.

Preparation does not require an idle live instance, an available Remote,
shared-token credentials or a running API. Its isolated probes never activate
providers or connectors. It never stops, starts, restarts, reloads or reboots
Core, Functions, Workers, Remote, Workbench or the computer.

The user chooses when to adopt. Ordinary Function updates use the existing
`/reboot` controls; shared Functions use `/reboot max`. Core and Remote retain
their separate maintenance lifecycles. Check `/version full` after adoption.
A readiness receipt is dated evidence, not permission to skip later source,
runtime, busy-state or adoption checks.

## Existing complete update

The default `hashi update` workflow retains its healthy/idle runtime and
independently supervised Remote preflight. After the same exact fast-forward
and source gate it re-executes the fetched `deploy` coordinator, asks Remote
to cold-restart Core, re-registers and restarts Remote, then verifies the complete
Worker set and all component versions. Partial or mixed adoption returns failure.

## Failure and recovery

Failures return a non-zero result naming the failed phase. A failed preparation
never returns readiness or an activation claim. Errors after source advancement
include the previous and target commit, state that no restart occurred, and
retain the updated source for investigation. The command never performs an
automatic reset or rewind, which could discard concurrent work or cross a
migration. Resolve the cause and retry; an unchanged target is requalified.

Existing configuration, credentials, databases, sessions and runtime selectors
stay in place. Packaged installs remain a separate verified release channel;
these source commands do not convert them into Git checkouts. A source/root
permission error is reported, without widening ordinary Agent tool permissions
or disguising a protected write as a read.
