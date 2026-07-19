# Adjacent Git Sync (Operator ↔ Target)

## Goal

Move code between operator integration tree and target worktree over
**adjacent-network Git** (LAN bare remote now; VPN-capable later).

**Non-dependencies:** MEGA, OneDrive are not transport for this EXP.

## Lab workflow (mother ↔ HP)

### Operator → target

1. Clean or intentional commits on operator machine.
2. `git push <adjacent-remote> HEAD:main`
3. `git push <adjacent-remote> HEAD:device/<device-id>` when device branch used.
4. On target: `git fetch`
5. If dirty worktree: `git stash push -u -m "before-sync-..."` (preserve local).
6. `git checkout` device branch; `git reset --hard origin/<device-branch>`
7. Refresh runtime entry copies if needed (e.g. `Launch-Aptenra.ps1` from
   `scripts/Start-AptenraDebugDesktop.ps1`).

### Target → operator

1. Commit on device branch with clear message and evidence note.
2. Push to adjacent bare (or bundle if offline policy applies).
3. Operator fetches, reviews, merges/cherry-picks into integration branch.
4. Operator remains integration gate.

## Safety

- Never commit secrets, PINs, chat logs, or full `.env` secrets.
- Stash before hard reset; record stash ref in evidence.
- Do not run production from OneDrive-synced trees.

## Client onsite note

Customer sites may not use the lab bare remote. Same **discipline** applies:
commit identity, adjacent transport, no cloud-drive source of truth. Exact
remotes are session-specific.
