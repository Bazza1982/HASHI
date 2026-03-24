#!/usr/bin/env python3
"""
HASHI Memory Consolidation Script (Layer 3)

Guardian: Lily (小蕾) — the sole memory consolidator.

Scans all HASHI instances for agents with memory_sync=on,
classifies memories into 4 domains, and incrementally writes
them into consolidated_memory.sqlite.

Phase 1: Schema + scan + classify + deduplicated insert + logging
Phase 2: BGE-M3 embedding batch fill (--embed flag)

Usage:
    python consolidate_memory.py           # Phase 1 only: scan + insert
    python consolidate_memory.py --embed   # Phase 1 + Phase 2: also fill embeddings
"""

import sqlite3
import json
import os
import sys
import shutil
import struct
import time
import numpy as np
from datetime import datetime, timezone

# ── Paths ──────────────────────────────────────────────────────
LILY_WORKSPACE = "/home/lily/projects/hashi/workspaces/lily"
CONSOLIDATED_DB = os.path.join(LILY_WORKSPACE, "consolidated_memory.sqlite")
CONSOLIDATION_LOG = os.path.join(LILY_WORKSPACE, "consolidation_log.jsonl")

# BGE-M3 paths (Phase 2)
BGE_MODEL_PATH = "/mnt/c/Users/thene/.cache/bge-m3-onnx-npu/bge-m3-int8.onnx"
BGE_TOKENIZER = "BAAI/bge-m3"
BGE_DIM = 1024
BGE_BATCH_SIZE = 16
BGE_MAX_LENGTH = 512

# Instances to scan (order matters: WSL direct first, Windows via /mnt last)
INSTANCES = [
    {"name": "HASHI1", "path": "/home/lily/projects/hashi", "cross_fs": False},
    {"name": "HASHI2", "path": "/home/lily/projects/hashi2", "cross_fs": False},
    {"name": "HASHI9", "path": "/mnt/c/Users/thene/projects/HASHI", "cross_fs": True},
]

# ── Noise filters ──────────────────────────────────────────────
NOISE_PREFIXES = [
    "--- AGENT FYI ---",
    "--- SKILL CONTEXT",
    "SYSTEM: Start a fresh session",
]
MIN_CONTENT_LENGTH = 50

# ── Domain classification keywords ────────────────────────────
DOMAIN_KEYWORDS = {
    "identity": [
        "人设", "personality", "role change", "升职", "character",
        "灵魂", "soul", "identity", "自我介绍", "agent.md update",
    ],
    "project": [
        "aipm", "aisheet", "sprint", "phase", "implement",
        "deploy", "milestone", "deadline", "release",
    ],
    "system": [
        "agent.md", "config", "hashi", "bridge", "command",
        "/reset", "workspace", "port", "restart", "backend",
        "openclaw", "gateway", "telegram", "token", "pid",
        "memory_sync", "/dream", "/memory", "cron", "heartbeat",
        "task scheduler", "autostart", "gitignore", "sqlite",
        "agents.json", "secrets.json", "skill_state",
    ],
    # "personal" is the fallback — no keywords needed
}


def classify_domain(content: str) -> str:
    """Classify a memory into one of 4 domains by keyword match."""
    lower = content.lower()
    # Check identity first (rare but specific)
    for kw in DOMAIN_KEYWORDS["identity"]:
        if kw in lower:
            return "identity"
    # Project next
    for kw in DOMAIN_KEYWORDS["project"]:
        if kw in lower:
            return "project"
    # System
    for kw in DOMAIN_KEYWORDS["system"]:
        if kw in lower:
            return "system"
    return "personal"


def is_noise(content: str) -> bool:
    """Return True if content is noise that should be skipped."""
    if len(content) < MIN_CONTENT_LENGTH:
        return True
    for prefix in NOISE_PREFIXES:
        if content.lstrip().startswith(prefix):
            return True
    return False


def init_consolidated_db(db_path: str) -> sqlite3.Connection:
    """Create or open the consolidated memory database."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS consolidated (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            instance        TEXT    NOT NULL,
            agent_id        TEXT    NOT NULL,
            source_id       INTEGER NOT NULL,
            domain          TEXT    NOT NULL,
            memory_type     TEXT    NOT NULL,
            importance      REAL    NOT NULL DEFAULT 1.0,
            content         TEXT    NOT NULL,
            summary         TEXT,
            embedding       BLOB,
            source_ts       TEXT    NOT NULL,
            consolidated_at TEXT    NOT NULL,
            UNIQUE(instance, agent_id, source_id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_domain
        ON consolidated(domain)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent
        ON consolidated(agent_id, domain)
    """)
    conn.commit()
    return conn


def get_existing_keys(conn: sqlite3.Connection) -> set:
    """Load all (instance, agent_id, source_id) already consolidated."""
    rows = conn.execute(
        "SELECT instance, agent_id, source_id FROM consolidated"
    ).fetchall()
    return set(rows)


def find_sync_agents(instance: dict) -> list:
    """Return list of (agent_id, db_path) for agents with memory_sync=on."""
    ws_dir = os.path.join(instance["path"], "workspaces")
    if not os.path.isdir(ws_dir):
        return []
    agents = []
    for agent in sorted(os.listdir(ws_dir)):
        ss = os.path.join(ws_dir, agent, "skill_state.json")
        db = os.path.join(ws_dir, agent, "bridge_memory.sqlite")
        if not os.path.isfile(ss) or not os.path.isfile(db):
            continue
        try:
            with open(ss) as f:
                state = json.load(f)
            if state.get("memory_sync"):
                agents.append((agent, db))
        except (json.JSONDecodeError, IOError):
            continue
    return agents


def read_memories(db_path: str, cross_fs: bool) -> list:
    """Read all memories from a bridge_memory.sqlite.
    For cross-filesystem DBs (Windows), copy to /tmp first."""
    actual_path = db_path
    tmp_path = None
    if cross_fs:
        tmp_path = f"/tmp/hashi_consolidate_{os.getpid()}_{id(db_path)}.sqlite"
        shutil.copy2(db_path, tmp_path)
        actual_path = tmp_path
    try:
        conn = sqlite3.connect(actual_path)
        rows = conn.execute(
            "SELECT id, ts, memory_type, importance, content FROM memories"
        ).fetchall()
        conn.close()
        return rows
    except Exception as e:
        print(f"  ERROR reading {db_path}: {e}")
        return []
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


def consolidate():
    """Main consolidation routine."""
    now = datetime.now(timezone.utc).isoformat()
    print(f"=== Memory Consolidation — {now} ===\n")

    conn = init_consolidated_db(CONSOLIDATED_DB)
    existing = get_existing_keys(conn)
    print(f"Existing consolidated records: {len(existing)}\n")

    stats = {
        "timestamp": now,
        "scanned_agents": 0,
        "scanned_memories": 0,
        "new_inserted": 0,
        "skipped_existing": 0,
        "skipped_noise": 0,
        "errors": 0,
        "by_domain": {"personal": 0, "system": 0, "project": 0, "identity": 0},
        "by_instance": {},
    }

    for instance in INSTANCES:
        inst_name = instance["name"]
        print(f"--- {inst_name} ({instance['path']}) ---")
        stats["by_instance"][inst_name] = {"agents": 0, "new": 0}

        agents = find_sync_agents(instance)
        if not agents:
            print("  No sync-on agents found.\n")
            continue

        for agent_id, db_path in agents:
            stats["scanned_agents"] += 1
            stats["by_instance"][inst_name]["agents"] += 1

            memories = read_memories(db_path, instance["cross_fs"])
            agent_new = 0

            for row in memories:
                source_id, ts, mem_type, importance, content = row
                stats["scanned_memories"] += 1
                key = (inst_name, agent_id, source_id)

                if key in existing:
                    stats["skipped_existing"] += 1
                    continue

                if is_noise(content):
                    stats["skipped_noise"] += 1
                    continue

                domain = classify_domain(content)

                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO consolidated
                           (instance, agent_id, source_id, domain,
                            memory_type, importance, content,
                            source_ts, consolidated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (inst_name, agent_id, source_id, domain,
                         mem_type, importance, content, ts, now),
                    )
                    stats["new_inserted"] += 1
                    stats["by_domain"][domain] += 1
                    agent_new += 1
                    existing.add(key)
                except Exception as e:
                    stats["errors"] += 1
                    print(f"  ERROR inserting {key}: {e}")

            print(f"  {agent_id}: {len(memories)} scanned, {agent_new} new")
            stats["by_instance"][inst_name]["new"] += agent_new

        conn.commit()
        print()

    conn.close()

    # Write log entry
    with open(CONSOLIDATION_LOG, "a") as f:
        f.write(json.dumps(stats, ensure_ascii=False) + "\n")

    # Print summary
    print("=" * 50)
    print(f"Scanned: {stats['scanned_agents']} agents, {stats['scanned_memories']} memories")
    print(f"New:     {stats['new_inserted']}")
    print(f"Skipped: {stats['skipped_existing']} existing, {stats['skipped_noise']} noise")
    print(f"Errors:  {stats['errors']}")
    print(f"Domains: {stats['by_domain']}")
    print(f"DB:      {CONSOLIDATED_DB}")
    print(f"Log:     {CONSOLIDATION_LOG}")

    return stats


# ── Phase 2: BGE-M3 Embedding ─────────────────────────────────

def load_bge():
    """Load BGE-M3 INT8 model and tokenizer. Returns (session, tokenizer)."""
    import onnxruntime as ort
    from transformers import AutoTokenizer

    print(f"Loading BGE-M3 from {BGE_MODEL_PATH}...")
    t0 = time.time()
    session = ort.InferenceSession(BGE_MODEL_PATH, providers=["CPUExecutionProvider"])
    tokenizer = AutoTokenizer.from_pretrained(BGE_TOKENIZER, trust_remote_code=True)
    print(f"BGE-M3 loaded in {time.time()-t0:.1f}s")
    return session, tokenizer


def embed_batch(session, tokenizer, texts: list) -> list:
    """Encode a batch of texts into 1024-dim embeddings. Returns list of bytes."""
    inputs = tokenizer(
        texts, padding=True, truncation=True,
        max_length=BGE_MAX_LENGTH, return_tensors="np",
    )
    outputs = session.run(None, {
        "input_ids": inputs["input_ids"].astype(np.int64),
        "attention_mask": inputs["attention_mask"].astype(np.int64),
    })
    # outputs[1] is the pooled 1024-dim sentence embedding
    pooled = outputs[1]  # shape: (batch, 1024)
    # Normalize
    norms = np.linalg.norm(pooled, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1, norms)
    pooled = pooled / norms
    # Convert each row to bytes (float32)
    return [row.astype(np.float32).tobytes() for row in pooled]


def embed_consolidated():
    """Fill embedding column for all records that don't have one yet."""
    conn = sqlite3.connect(CONSOLIDATED_DB)
    # Count how many need embedding
    total = conn.execute(
        "SELECT COUNT(*) FROM consolidated WHERE embedding IS NULL"
    ).fetchone()[0]

    if total == 0:
        print("All records already have embeddings. Nothing to do.")
        conn.close()
        return {"embedded": 0, "errors": 0}

    print(f"\n=== Phase 2: BGE-M3 Embedding — {total} records to encode ===\n")

    session, tokenizer = load_bge()

    # Process in batches
    embedded = 0
    errors = 0
    offset = 0

    while True:
        rows = conn.execute(
            "SELECT id, content FROM consolidated WHERE embedding IS NULL "
            "ORDER BY id LIMIT ?", (BGE_BATCH_SIZE,)
        ).fetchall()
        if not rows:
            break

        ids = [r[0] for r in rows]
        # Extract meaningful text (first 1500 chars of content)
        texts = [r[1][:1500] for r in rows]

        try:
            embeddings = embed_batch(session, tokenizer, texts)
            for rid, emb in zip(ids, embeddings):
                conn.execute(
                    "UPDATE consolidated SET embedding = ? WHERE id = ?",
                    (emb, rid),
                )
            conn.commit()
            embedded += len(rows)
            print(f"  Encoded {embedded}/{total} ({embedded*100//total}%)")
        except Exception as e:
            errors += len(rows)
            print(f"  ERROR encoding batch starting at id={ids[0]}: {e}")
            # Mark these as skipped by moving past them
            conn.commit()
            break

    conn.close()

    stats = {"embedded": embedded, "errors": errors}
    print(f"\nPhase 2 complete: {embedded} encoded, {errors} errors")

    # Append to log
    with open(CONSOLIDATION_LOG, "a") as f:
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "phase": "embed",
            **stats,
        }
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

    return stats


if __name__ == "__main__":
    consolidate()
    if "--embed" in sys.argv:
        embed_consolidated()
