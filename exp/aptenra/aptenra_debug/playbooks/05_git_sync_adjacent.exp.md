# Adjacent Git Sync (Operator ↔ Target)

## Goal

Move code between operator integration tree and target worktree over
**adjacent-network Git** (LAN bare remote now; VPN-capable later).

**Non-dependencies:** MEGA, OneDrive are not transport for this EXP.

## Lab workflow (mother ↔ HP)

### Operator → target

1. Work on an intentional integration branch on the operator machine and
   require a clean worktree before promotion.
2. Fetch the adjacent remote and prove both remote refs are ancestors of the
   reviewed integration commit. A non-fast-forward result is a review stop,
   never a reason to force-push.
3. Push the same reviewed commit to `main` and `device/<device-id>` using
   ordinary fast-forward pushes.
4. On the target, run `git fetch` and require a clean worktree. If it is
   dirty, commit the target work to a preservation/device branch and return it
   to the operator; do not auto-stash or discard it.
5. Update with `git merge --ff-only origin/device/<device-id>`.
6. Verify `HEAD`, `origin/main`, and `origin/device/<device-id>` resolve to the
   promoted commit before building or deploying anything.
7. User-facing Aptenra must be installed from the immutable MSI/release
   manifest built at that commit. Do not refresh a live product by copying a
   launcher, source file, or helper binary from the worktree.

### Target → operator

1. Commit on device branch with clear message and evidence note.
2. Push to adjacent bare (or bundle if offline policy applies).
3. Operator fetches, reviews, and uses a real merge into an integration
   branch. Preserve the device commits and their ancestry; do not cherry-pick
   normal two-machine development.
4. Run the combined acceptance gates, build one immutable release artifact
   set, and then fast-forward both `main` and the device branch to that merge.
5. Operator remains the integration and release gate.

## Safety

- Never commit secrets, PINs, chat logs, or full `.env` secrets.
- Never use `reset --hard`, force-push, or an automatic stash as a normal sync
  mechanism. Preserve dirty work explicitly and stop for review.
- Never deploy `integration/*` directly. Promote a complete reviewed commit to
  both authoritative refs first.
- A successful source sync is not a product deployment. Formal and Debug must
  use the same manifest-bound artifact hashes from one clean build.
- Do not run production from OneDrive-synced trees.

## Client onsite note

Customer sites may not use the lab bare remote. Same **discipline** applies:
commit identity, adjacent transport, no cloud-drive source of truth. Exact
remotes are session-specific.
