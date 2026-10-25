# HASHI source release closure

## Decision

A Git source update is complete only when the exact fetched commit is active in
the HASHI Core, shared Functions, every configured Function Worker, and the
independently supervised Hashi Remote process. Moving the checkout alone is not
a deployment, and a partial adoption must return failure.

The supported operator command is:

```text
hashi --instance <name> update --confirm <name>
```

It defaults to `origin/main`. `--remote` and `--branch` may select another
explicit release source. This command is intentionally unavailable for packaged
installs; those must adopt one complete verified package.

## Closed-loop sequence

The update command performs one coordinated operation:

1. Resolve the exact registered instance and require its exact name as
   confirmation.
2. Require a clean Git checkout, a healthy idle Core, verified Agent and
   background-job activity, and a reachable independently supervised Remote
   with the fixed Core-restart capability.
3. Fetch the requested remote branch and allow only a fast-forward to the exact
   fetched commit.
4. Run the protected-Core release gate, then re-execute the release coordinator
   from the fetched checkout.
5. Keep Remote alive while it performs a durable, terminally verified cold
   restart of HASHI Core. The new Core starts Functions and all Workers from the
   fetched source.
6. Re-register the Remote supervisor from the fetched source and restart Remote
   itself.
7. Poll the structured version endpoint and succeed only when source, Core,
   Functions, every active Worker, and Remote all report the exact target
   commit, every Worker is active and accepting, the source is clean, and the
   aggregate state is `running_matches_source`.

Any failed or unverified phase returns a non-zero result naming that phase.
The command never reports success for a source-only update or mixed generation.
It does not automatically rewind source after an adoption failure; automatic
rewind could cross migrations or discard valid state. After correcting the
reported cause, rerunning the same command safely targets the same commit and
closes the remaining adoption.

## Remote independence

Remote remains a separately supervised rescue program. Ordinary
`/reboot min|same|max` continues to manage Function/Worker generations and
does not restart Remote.

That independence is what lets Remote restart a stopped or unhealthy Core. It
is not an exemption from release adoption. A complete HASHI source release
therefore uses the old Remote to restart Core, then explicitly adopts and
verifies the new Remote before the release can succeed.

## Safety boundaries

- No merge commit, force update, hard reset, branch creation, or worktree is
  performed.
- A dirty checkout, non-fast-forward target, busy or unverifiable runtime,
  missing Remote supervision, failed Core receipt, offline component, missing
  provenance, or commit mismatch blocks success.
- Instance configuration, credentials, databases, sessions, and runtime data
  remain in place; the command advances only the registered source checkout and
  invokes the existing fixed lifecycle controllers.
- Published packages remain a separate release channel and are never converted
  into a Git checkout by this command.
