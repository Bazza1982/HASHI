# HASHI source preparation and release adoption

PAO owns source deployment in Functions and platform configuration. Protected
Core, runtime selectors and ordinary Agent tool protection are unchanged.

## Operator commands and result states

| Command | Result | Live actions |
|---|---|---|
| `hashi --instance <name> deploy --confirm <name>` | Ready to reboot | None |
| `hashi --instance <name> update --stage-only --confirm <name>` | Ready to reboot | None |
| `/hashideploy` | Ready to reboot | None |
| `/hashideploy force` | Ready to reboot | None |
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
   A clean checkout left on another branch is accepted only while its HEAD is
   an ancestor of the fetched target; it is then checked out at the requested
   branch and exact commit instead of advancing the unrelated branch. Dirty or
   diverged checkouts are refused without fetching, moving or deleting
   anything, and the reply names the branch and both commits.

    `/hashideploy force` is the explicit exception, available to the human
    owner only: automated HAL dispatch cannot run it. It fetches first (a
    network failure changes nothing), stashes tracked and untracked changes
    with `git stash push --include-untracked` (never ignored files), refuses
    to continue if anything remains, records a `refs/hashideploy-backups/...`
    ref whenever the old HEAD is not part of the target history, and only then
    checks out
    `main` at the fetched commit. Local commits are never reset away and
    nothing is force-pushed. The readiness receipt records the stash commit,
    the backup ref and the previous branch/commit. All source, protected-Core,
    runtime-pair, qualification and receipt gates still run; force changes how
    the checkout is aligned, not what must pass.
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

The slash command retains its preparation task until the actual work finishes.
Cancelling the command or interrupting a notification does not stop the source
operation or admit a second preparation. Completion releases the busy state
and records the final result in the log even without a reply recipient. To
reconcile an interrupted reply, compare the checkout HEAD with the exact target
in its persisted readiness receipt; cancellation alone does not establish
success or failure, and absence of a matching receipt is not readiness.

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

Failures return a non-zero result and a human-readable reply. The reply names
one real failure class: instance source/Git configuration, dirty checkout,
fetch, non-fast-forward, protected gate, runtime/toolchain, commit mismatch,
receipt/state-I/O, or force recovery. It always states whether the checkout had
already moved, with the relevant commit, and never prints an internal phase
identifier or a raw `??` status line. Force failures additionally report the
stash commit and backup ref so nothing is lost.

A failed preparation never returns readiness or an activation claim. Errors
after source advancement include the previous and target commit, state that no
restart occurred, and retain the updated source for investigation. The command
never performs an automatic reset or rewind, which could discard concurrent
work or cross a migration. Resolve the cause and retry; an unchanged target is
requalified.

Existing configuration, credentials, databases, sessions and runtime selectors
stay in place. Packaged installs remain a separate verified release channel;
these source commands do not convert them into Git checkouts. A source/root
permission error is reported, without widening ordinary Agent tool permissions
or disguising a protected write as a read.
