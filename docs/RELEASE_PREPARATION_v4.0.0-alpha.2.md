# HASHI v4.0.0-alpha.2 Release Preparation Record

Date: 2026-08-16

Status: **local release candidate consolidated and verified; tag and GitHub
push intentionally pending**

This record covers the broader HASHI v4 platform line. It does not change the
independent Enterprise AAI package version `0.1.0a1`.

## Consolidated Scope

The release candidate preserves the complete reviewed HER `.22` development
line and its follow-on HASHI work, including:

- certified Linux and Windows HER `0.1.0-hashi.22` packages and provenance;
- TaskFrame planning, finalization, evidence, review, commentary, stream, and
  session fixes accumulated through the `.22` line;
- direct-conversation continuity, scheduler isolation, reply-target snapshots,
  delivery ordering, and event-ID idempotency;
- all seven HER effort choices, including adapter-owned `ultra` coordination;
- secure media, Tool Gateway/MCP, Habit/Meditation, and Dream integration;
- source-integrated `/rebuild`, candidate adoption, cache correction, live
  verification documentation, and rollback safeguards;
- the canonical Workbench Agent Overview and shared-token-authenticated remote
  terminal work that existed on separate clean feature branches.

The original feature branches and pre-consolidation refs remain available for
audit. Consolidation does not delete or rewrite their commits.

## Verification Snapshot

| Gate | Result |
| --- | --- |
| HASHI Python suite | `2285 passed`, `2 skipped` |
| Integrated HER Rust suite | `1481` tests discovered; full workspace/all-target run passed |
| Agent Overview + remote shared-token focus | `32 passed` |
| Python static compile | passed |
| Protected-core manifest | passed |
| Packaged HER version probe | passed; selected Linux `.22` identity verified |
| Updated Markdown relative links | passed |
| Git whitespace/diff check | passed |

The Python suite must be invoked with `--ignore=workspaces` in a lived-in HASHI
checkout. Local ignored Agent workspaces can contain nested repository copies
whose duplicate module names confuse pytest collection; they are absent from a
clean clone.

The Rust run emitted only the existing test-target unused-variable warnings
already governed by HER certification evidence. No Rust test failed.

## Publication Hygiene Review

The exact outbound commit history and current release-candidate tree were
checked with redacted high-confidence patterns for private keys, GitHub/AWS/
OpenAI-style/Slack/Telegram credentials, credential-bearing URLs, live account
identifiers, private network literals, personal filesystem roots, and generic
credential assignments.

Results:

- no private key, live access token, bot token, credential URL, or tracked live
  `.env` / `secrets.json` / `agents.json` was found;
- one source-history match is an intentionally fake API-key-shaped value in a
  Rust test proving session persistence redaction;
- all other credential-assignment matches are explicit fixtures, examples, or
  redaction tests;
- two historical documentation examples containing a developer-local home
  path were replaced with neutral `/path/to/...` placeholders;
- local runtime state, logs, workspaces, rebuild candidates, backups, and
  operator-only files are ignored and are not part of the outbound range;
- packaged binaries contain compiler/test fixture path strings, but the set of
  embedded home-directory identities is unchanged from the existing upstream
  GitHub history, and no credential pattern was found in the new artifacts;
- the newly added HER release payload is approximately 321 MiB in the working
  tree; its largest single file is the approximately 22.6 MiB certified source
  bundle, below GitHub's 100 MiB single-file limit.

The compiler-path observation is not a credential leak and introduces no new
local identity in this release. A future release-build hardening task may use
Rust path-prefix remapping and stripped reproducible symbols, but changing the
already certified `.22` binaries would require a new package identity and full
cross-platform recertification.

## Intentionally Not Performed

- no Git tag was created;
- no branch or release artifact was pushed to GitHub;
- no remote history was rewritten;
- no Aptenra file, branch, worktree, or runtime was modified;
- no ignored local operator data was deleted.

Before publication, review the final `main` tip and exact
`origin/main..main` range, confirm the GitHub destination, and obtain explicit
approval for the tag and push.
