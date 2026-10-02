# Remote Memory Consolidation Rollout

> Lily central import update (2026-10-02): its authoritative database, inbox,
> import ACKs, and daily writer moved to the
> [independent Windows installation](LILY_MEMORY_WIKI_INDEPENDENCE_2026-10-02.md).
> The old HASHI4 commands below are retained as historical rollout examples;
> remote sources may still use the old landing directory through a read-only
> compatibility adapter until direct delivery changes separately.

Status: implemented package baseline for `auto_vibe_code` superloop.

## Package Entry

Use the standalone script:

```bash
python3 scripts/remote_memory_consolidation.py --root /path/to/hashi diagnose
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

- CPython 3.12.13 (approved HASHI Core runtime)
- Python standard library only for the package entry itself
- Remote export and central import use the script's built-in privacy filter.
  The result is the same whether or not the Wiki package is installed.

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
  "vault_root": "/path/to/your/wiki-vault",
  "mirror_root": "/path/to/hashi/private/remote_wiki_mirror",
  "consolidated_db": "/path/to/hashi/workspaces/lily/consolidated_memory.sqlite"
}
```

Relative paths in this file resolve from `--root`. Without an explicit
`vault_root`, the script uses `<root>/wiki`.

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
python3 scripts/remote_memory_consolidation.py --root /path/to/hashi promote-incoming --check
python3 scripts/remote_memory_consolidation.py --root /path/to/hashi promote-incoming
python3 scripts/remote_memory_consolidation.py --root /path/to/hashi import --check
python3 scripts/remote_memory_consolidation.py --root /path/to/hashi import --acknowledge --destination-instance-id HASHI4
```

The legacy HASHI4 compatibility task used an installed script outside the
mutable checkout and passed `--root` only for instance data and configuration.
The independent Memory/Wiki installation now owns its executable path and
schedule. Source pending batches remain untouched until an independently
verified source-side acknowledgement.

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
python3 scripts/remote_memory_consolidation.py --root /path/to/hashi sync-wiki --dry-run
python3 scripts/remote_memory_consolidation.py --root /path/to/hashi sync-wiki --check
python3 scripts/remote_memory_consolidation.py --root /path/to/hashi sync-wiki
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
