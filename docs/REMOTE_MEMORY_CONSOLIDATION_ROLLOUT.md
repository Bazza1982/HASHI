# Remote Memory Consolidation Rollout

Status: implemented package baseline for `auto_vibe_code` superloop.

## Package Entry

Use the standalone script:

```bash
python3 scripts/remote_memory_consolidation.py --root /home/lily/projects/hashi diagnose
```

It does not require HASHI runtime command wiring or reboot.

## Remote Startup Flow

On INTEL/MSI:

```bash
cd /path/to/hashi
python3 scripts/remote_memory_consolidation.py --root /path/to/hashi export --instance-id INTEL --agent sakura --check
python3 scripts/remote_memory_consolidation.py --root /path/to/hashi export --instance-id INTEL --agent sakura
```

For MSI, use the MSI instance id and the local agent ids:

```bash
cd /path/to/hashi
python3 scripts/remote_memory_consolidation.py --root /path/to/hashi export --instance-id MSI --agent ying --check
python3 scripts/remote_memory_consolidation.py --root /path/to/hashi export --instance-id MSI --agent ying
```

The batch is written under:

```text
private/remote_memory_export/pending/<batch_id>/
```

Deliver that directory to Lily's secure inbox by Hashi Remote file transfer,
rsync/SSH/Tailscale, SMB/Syncthing drop, or manual copy:

```text
private/remote_memory_inbox/<INSTANCE>/<AGENT>/<batch_id>/
```

`/hchat` should only carry control/ack messages, not bulk payloads.

If the central inbox is mounted as a LAN drop folder, use the package delivery
command:

```bash
python3 scripts/remote_memory_consolidation.py \
  --root /path/to/hashi \
  deliver \
  --target-inbox /mnt/lily_drop/private/remote_memory_inbox \
  --dry-run

python3 scripts/remote_memory_consolidation.py \
  --root /path/to/hashi \
  deliver \
  --target-inbox /mnt/lily_drop/private/remote_memory_inbox
```

Delivery copies manifest + payload and leaves the local `pending/` batch in
place until Lily acknowledges import.

## Minimal Dependencies

- Python 3.10+
- Python standard library only for the package entry itself
- Optional: existing HASHI `scripts.wiki.fetcher` privacy helpers are reused
  when available; otherwise the script falls back to conservative local checks.
- Remote export and central import both run privacy filters. When full wiki
  helpers are unavailable on a remote machine, the package uses built-in
  conservative fallback patterns and Lily central import performs the full
  privacy gate again.

No HASHI bot runtime, Telegram command registration, scheduler reload, or
reboot is required to run export/import/sync directly.

## Private Config Template

Create this file locally if paths differ. It must stay under `private/`.

```json
{
  "export_root": "/path/to/hashi/private/remote_memory_export",
  "inbox_root": "/path/to/hashi/private/remote_memory_inbox",
  "accepted_store": "/path/to/hashi/private/remote_memory_accepted/accepted_records.jsonl",
  "quarantine_root": "/path/to/hashi/private/remote_memory_quarantine",
  "logs_root": "/path/to/hashi/logs",
  "vault_root": "/mnt/c/Users/thene/Documents/lily_hashi_wiki",
  "mirror_root": "/path/to/hashi/private/remote_wiki_mirror",
  "consolidated_db": "/path/to/hashi/workspaces/lily/consolidated_memory.sqlite"
}
```

Validate installation:

```bash
python3 scripts/remote_memory_consolidation.py --root /path/to/hashi diagnose
python3 scripts/remote_memory_consolidation.py --root /path/to/hashi export --instance-id INTEL --agent sakura --check
```

Expected signal:

```text
[remote-memory] success=true
```

## Lily Central Import

On Lily PC:

```bash
python3 scripts/remote_memory_consolidation.py --root /home/lily/projects/hashi import --check
python3 scripts/remote_memory_consolidation.py --root /home/lily/projects/hashi import --dry-run
python3 scripts/remote_memory_consolidation.py --root /home/lily/projects/hashi import
```

Accepted records are inserted into Lily's `consolidated_memory.sqlite` with:

- `instance=<remote instance>`
- `agent_id=<remote agent>`
- `domain=remote_memory`
- `memory_type=episodic`
- `ts_source=remote:<batch_id>:<source_kind>:<source_path>`

The existing wiki pipeline then sees them as normal consolidated rows.

## Read-Only Wiki Mirror

Sync only generated zones:

```bash
python3 scripts/remote_memory_consolidation.py --root /home/lily/projects/hashi sync-wiki --dry-run
python3 scripts/remote_memory_consolidation.py --root /home/lily/projects/hashi sync-wiki --check
python3 scripts/remote_memory_consolidation.py --root /home/lily/projects/hashi sync-wiki
```

Copied zones:

- `10_GENERATED_TOPICS`
- `30_GENERATED_INDEXES`
- `00_SYSTEM`

Human notes are not copied.

## Quality Gates

- All mutating modes support `--check` or `--dry-run` where applicable.
- Payloads are gzip JSONL with SHA256 manifest verification.
- Import is idempotent through the consolidated DB unique key.
- Bad schema/checksum/private-content batches are quarantined.
- Runtime core command/scheduler files are not required for this package.
