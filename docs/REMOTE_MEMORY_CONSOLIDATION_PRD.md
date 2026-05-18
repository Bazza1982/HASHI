# Remote Memory Consolidation PRD

Status: implemented standalone package baseline; architecture PRD remains the product reference.
Owner: HASHI memory/wiki pipeline
Created: 2026-05-16

## 1. Summary

HASHI needs a reliable way to consolidate memory from remote instances such as
INTEL and MSI into the central Lily wiki, even when those machines are offline
for long periods.

The recommended product direction is:

```text
Remote HASHI instances
  -> durable local export queue
  -> secure central inbox on Lily PC when online
  -> central import / validation / dedupe
  -> Lily wiki pipeline
  -> central generated wiki
  -> read-only wiki mirror/cache for remote instances
```

The Lily PC remains the canonical wiki authority. Remote machines do not publish
their own authoritative wiki pages into the shared vault. They may keep a local
read-only mirror/cache of the generated wiki for offline use.

## 2. Problem

Current HASHI memory consolidation is local-machine oriented. This creates gaps:

1. Remote agents may hold important memory in local logs that never reaches the
   central wiki.
2. Remote machines are not always online, so a pull-only daily cron on Lily
   cannot assume they are reachable.
3. If each remote machine builds its own wiki, knowledge becomes fragmented and
   must still be synchronized later.
4. A shared LAN/NAS wiki folder with multiple writers creates conflict,
   partial-write, privacy, and publish-order risks.
5. `/wiki` needs a stable knowledge source. If different instances point to
   different local wikis, answers will diverge.

## 3. Product Goals

1. Consolidate remote HASHI memory into Lily's central wiki without requiring
   remote machines to be online during the daily wiki cron.
2. Preserve one canonical generated wiki owned by the Lily PC.
3. Let remote machines upload memory safely when they come online.
4. Let remote machines use `/wiki` even when offline by reading a last-known
   good generated wiki cache.
5. Make every transfer auditable, idempotent, and diagnosable.
6. Avoid direct multi-writer writes to the Obsidian vault.

## 4. Non-Goals

1. Do not make every remote HASHI instance an independent wiki authority.
2. Do not allow remote machines to write directly into live generated vault
   zones.
3. Do not use `/hchat` as the primary transport for large log payloads.
4. Do not require HASHI core runtime changes for the first implementation.
5. Do not sync secrets, API keys, sqlite databases, or private machine configs.

## 5. Users And Use Cases

### Primary Operator

The operator wants all HASHI agents across Lily PC, INTEL, MSI, and future
instances to share durable knowledge through one wiki.

### Remote Instance

A remote instance may be offline during Lily's daily memory consolidation. When
it comes online, it should export memory deltas and deliver them to Lily without
manual copy/paste.

### Agent Using `/wiki`

An agent should query the same knowledge base regardless of which machine it is
running on. If offline, it should query a local read-only cache of the last
published central wiki.

## 6. Recommended Architecture

### 6.1 Central Authority

The Lily PC is the canonical wiki authority.

Responsibilities:

- receive remote memory bundles;
- validate and quarantine incoming bundles;
- dedupe imported records;
- include approved remote records in the wiki classification pipeline;
- publish generated wiki zones using the existing staging-first publisher;
- produce manifests and audit logs;
- expose or sync read-only generated wiki output to remote instances.

### 6.2 Remote Export Queue

Each remote instance maintains a local export queue under private state.

Example:

```text
private/remote_memory_export/
  pending/
    batch_20260516_083000_INTEL_sakura.jsonl.zst
    batch_20260516_083000_INTEL_sakura.manifest.json
  sent/
  acked/
  failed/
```

Responsibilities:

- scan local agent memory/log sources;
- export only records after the last acknowledged watermark;
- package records into signed or hashed batches;
- retry delivery when online;
- keep local records until Lily acknowledges successful import.

### 6.3 Central Secure Inbox

The Lily PC receives batches into a secure inbox outside committed source code.

Example:

```text
private/remote_memory_inbox/
  INTEL/
    sakura/
      batch_20260516_083000/
        manifest.json
        records.jsonl.zst
        import.log
  MSI/
    ying/
      batch_20260516_091500/
```

The inbox is append-only from the remote sender's perspective. The central
importer owns validation, quarantine, and final acceptance.

### 6.4 Transport Boundary

`/hchat` should be a control plane, not the bulk data plane.

Use `/hchat` for:

- "I have a batch ready";
- "please pull batch X";
- acknowledgement summaries;
- failure notifications.

Use one of these for batch payloads:

- Hashi Remote file transfer endpoint;
- authenticated LAN HTTP endpoint;
- rsync over SSH/Tailscale;
- Syncthing/SMB drop folder with checksum validation;
- manual file copy as a fallback.

### 6.5 Wiki Consumption

Remote instances should read from:

1. central generated wiki when online;
2. local read-only mirror/cache when offline.

The mirror should only contain generated zones required by agents, such as:

```text
10_GENERATED_TOPICS/
30_GENERATED_INDEXES/
manifest files
```

Remote instances should not write into these generated zones.

## 7. Data Contract

### 7.1 Batch Manifest

Each remote batch must include:

```json
{
  "schema_version": 1,
  "batch_id": "20260516T083000Z_INTEL_sakura_000001",
  "instance_id": "INTEL",
  "agent_id": "sakura",
  "created_at": "2026-05-16T08:30:00Z",
  "source_files": [
    "workspaces/sakura/memory/left_brain_continuity.jsonl",
    "workspaces/sakura/transcript.jsonl"
  ],
  "record_count": 123,
  "first_source_id": "optional",
  "last_source_id": "optional",
  "previous_ack_watermark": "optional",
  "payload_file": "records.jsonl.zst",
  "payload_sha256": "...",
  "privacy_scan": {
    "status": "passed",
    "redacted_count": 0
  }
}
```

### 7.2 Record Envelope

Each record imported into Lily should be normalized:

```json
{
  "schema_version": 1,
  "source_instance": "INTEL",
  "source_agent": "sakura",
  "source_kind": "transcript|memory|left_brain_notepad|project_log",
  "source_path": "workspaces/sakura/transcript.jsonl",
  "source_record_id": "optional",
  "source_ts": "2026-05-16T08:29:12Z",
  "role": "user|assistant|system|thinking|note",
  "text": "...",
  "content_hash": "sha256:...",
  "metadata": {}
}
```

## 8. Functional Requirements

### R1. Remote Export

Remote instances must provide a script or sidecar mode that:

- prints the instance id, agent id, config path, export roots, and dry-run status;
- scans configured agent workspaces;
- exports only changed/new records since the last acknowledged watermark;
- writes a manifest and payload;
- computes payload hash;
- fails non-zero on unreadable source files or invalid output;
- supports `--check` and `--dry-run`.

### R2. Delivery

The delivery mechanism must:

- tolerate the central Lily PC being offline;
- retry safely;
- not delete local pending batches until central acknowledgement;
- log every attempt;
- support checksum verification.

### R3. Central Import

The Lily importer must:

- read only from configured secure inbox roots;
- validate manifest schema;
- validate payload checksum;
- reject or quarantine malformed batches;
- dedupe records by source identity and content hash;
- persist import state and watermarks;
- log accepted, duplicate, skipped, quarantined, and failed counts.

### R4. Wiki Pipeline Integration

The wiki pipeline must:

- include imported remote records as source material;
- preserve `source_instance` and `source_agent` metadata;
- maintain existing privacy, validation, classification, promotion, and publish
  gates;
- publish only through the staging-first generated-zone publisher;
- never write into human notes.

### R5. Remote `/wiki`

Remote `/wiki` configuration must support:

- online central wiki path or endpoint;
- local read-only mirror path;
- clear diagnostics showing which path was used;
- no silent fallback to stale or missing wiki content.

### R6. Read-Only Wiki Sync

The system should support syncing generated wiki zones from Lily to remote
instances.

The sync must:

- be one-way from Lily to remote;
- include manifest and version metadata;
- avoid syncing human notes unless explicitly configured;
- be safe to run repeatedly.

## 9. Logging And Diagnostics

Every script must log enough to diagnose remotely from one command output:

- mode: `check`, `dry-run`, `export`, `deliver`, `import`, `sync`;
- config paths;
- instance id and agent ids;
- source roots and destination paths;
- selected transport;
- current local watermark;
- central acknowledgement watermark;
- batch ids;
- record counts;
- privacy scan status;
- checksum status;
- success/failure with non-zero exit on failure.

Recommended logs:

```text
logs/remote_memory_export.jsonl
logs/remote_memory_delivery.jsonl
logs/remote_memory_import.jsonl
logs/remote_wiki_sync.jsonl
```

## 10. Security And Privacy Requirements

1. Inbox and export directories must live under `private/` or another ignored
   private state root.
2. `.gitignore` must exclude remote payloads, manifests with sensitive paths,
   sqlite databases, raw transcripts, secrets, and local configs.
3. Payload delivery should use authenticated transport.
4. Every payload must be checksummed.
5. Optional encryption should be supported for non-LAN delivery.
6. Privacy scan must run before central import acceptance.
7. Importer must fail closed on parse, checksum, privacy, or schema failures.

## 11. Success Criteria

The feature is successful when:

1. A remote instance can be offline during Lily's daily wiki cron and later
   upload missed memory without data loss.
2. Lily can import remote memory batches idempotently; re-importing the same
   batch creates no duplicate wiki source records.
3. Remote records appear in the central wiki pipeline with correct
   `source_instance` and `source_agent` metadata.
4. The central wiki publisher still writes only generated zones and produces
   manifest/backup/staging records.
5. `/wiki` on remote machines can use the central generated wiki when online
   and a read-only cache when offline.
6. No remote machine writes directly into the live central generated wiki.
7. Logs can prove:
   - what was exported;
   - what was delivered;
   - what was imported;
   - what was rejected or quarantined;
   - which wiki version was synced.
8. A full dry-run can be performed without modifying local or central state.
9. Failure modes are explicit and recoverable.

## 12. Phased Delivery

### Phase 0: Design And Config

- Add private config templates for remote export/import roots.
- Define manifest and record schemas.
- Add `.gitignore` coverage for remote memory payloads.

### Phase 1: Remote Export Script

- Build standalone export script.
- Support `--check`, `--dry-run`, and `--agent`.
- Generate batch manifest and payload.

### Phase 2: Central Import Script

- Build standalone importer.
- Validate schema, checksum, privacy, and dedupe.
- Store imported records in a central staging database or JSONL source store.

### Phase 3: Wiki Pipeline Source Integration

- Extend wiki fetcher to include accepted remote records.
- Preserve source metadata.
- Add tests for classification and publish path.

### Phase 4: Delivery Transport

- Start with filesystem drop or authenticated LAN endpoint.
- Use `/hchat` only for control/ack messages.
- Add retry and acknowledgement state.

### Phase 5: Remote Wiki Mirror

- Sync Lily generated zones to remote read-only cache.
- Add diagnostics for `/wiki` path selection.

### Phase 6: Cron / Task Integration

- Remote machines run export/deliver on startup and periodic cron.
- Lily runs import before daily wiki pipeline.
- Lily syncs generated wiki output after successful publish.

## 13. Operational Model

Daily central flow:

```text
remote import inbox
  -> validate / dedupe / accept
  -> memory consolidation
  -> wiki pipeline
  -> publish generated wiki
  -> sync read-only wiki mirrors
  -> acknowledge imported remote batches
```

Remote startup flow:

```text
remote boot
  -> export new memory
  -> deliver pending batches if Lily reachable
  -> receive acknowledgements
  -> pull latest generated wiki mirror
```

## 14. Open Questions

1. Should the first delivery transport be Hashi Remote file transfer, rsync over
   SSH/Tailscale, or a LAN drop folder?
2. Which remote sources are in scope for the first version:
   transcripts, memory store, project logs, left-brain notepad, or all of them?
3. Should remote machines run local emergency wiki generation when they remain
   offline for more than N days?
4. How should remote `/wiki` expose staleness warnings for offline cache use?
5. Should accepted remote records be stored in SQLite first, JSONL first, or both?

## 15. Recommended First Build

Build the smallest safe version:

1. Remote export script that writes batch manifest + compressed JSONL.
2. Central import script that validates, dedupes, and writes accepted records.
3. Wiki fetcher extension that reads accepted remote records.
4. One-way generated wiki sync to remote cache.

Do not start with direct shared-vault writing.
