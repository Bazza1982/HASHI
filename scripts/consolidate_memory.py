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

import hashlib
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
HABIT_EVAL_DB = os.path.join(LILY_WORKSPACE, "habit_evaluation.sqlite")
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

# Transcript turn sources to skip entirely (not worth storing)
TRANSCRIPT_NOISE_SOURCES = {"fyi", "system", "handoff", "startup"}
# Transcript sources that are kept but not shown as "user said"
TRANSCRIPT_SCHEDULER_SOURCES = {"scheduler", "cron"}
# Maximum chars to store per turn (BGE limit is 512 tokens ≈ 1500 chars)
TRANSCRIPT_MAX_CHARS = 3000

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
            ts_source       TEXT    NOT NULL DEFAULT 'legacy_unknown',
            consolidated_at TEXT    NOT NULL,
            UNIQUE(instance, agent_id, source_id)
        )
    """)
    cols = {
        row[1] for row in conn.execute("PRAGMA table_info(consolidated)").fetchall()
    }
    if "ts_source" not in cols:
        conn.execute(
            "ALTER TABLE consolidated ADD COLUMN ts_source TEXT "
            "NOT NULL DEFAULT 'legacy_unknown'"
        )
        conn.execute(
            "UPDATE consolidated SET ts_source = 'legacy_unknown' "
            "WHERE ts_source IS NULL OR ts_source = ''"
        )
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_domain
        ON consolidated(domain)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent
        ON consolidated(agent_id, domain)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_source_ts_quality
        ON consolidated(source_ts, ts_source)
    """)
    conn.commit()
    return conn


def init_habit_evaluation_db(db_path: str) -> sqlite3.Connection:
    """Create or open Lily's habit evaluation database."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS habit_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            version         INTEGER NOT NULL DEFAULT 1,
            instance        TEXT    NOT NULL,
            agent_id        TEXT    NOT NULL,
            habit_id        TEXT    NOT NULL,
            request_id      TEXT,
            task_type       TEXT,
            triggered       INTEGER NOT NULL DEFAULT 0,
            applied         INTEGER NOT NULL DEFAULT 0,
            helpful         INTEGER,
            harmful         INTEGER,
            ignored         INTEGER NOT NULL DEFAULT 0,
            context_summary TEXT,
            feedback_text   TEXT,
            feedback_ts     TEXT,
            ts              TEXT    NOT NULL,
            ts_source       TEXT    NOT NULL DEFAULT 'native',
            UNIQUE(instance, agent_id, habit_id, ts)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_habit_events_agent_ts
        ON habit_events(instance, agent_id, ts)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_habit_events_habit_ts
        ON habit_events(habit_id, ts)
    """)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(habit_events)").fetchall()}
    if "request_id" not in cols:
        conn.execute("ALTER TABLE habit_events ADD COLUMN request_id TEXT")
    if "feedback_text" not in cols:
        conn.execute("ALTER TABLE habit_events ADD COLUMN feedback_text TEXT")
    if "feedback_ts" not in cols:
        conn.execute("ALTER TABLE habit_events ADD COLUMN feedback_ts TEXT")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_habit_events_request
        ON habit_events(instance, agent_id, request_id, ts)
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
    """Return list of (agent_id, db_path, transcript_path) for agents with memory_sync=on."""
    ws_dir = os.path.join(instance["path"], "workspaces")
    if not os.path.isdir(ws_dir):
        return []
    agents = []
    for agent in sorted(os.listdir(ws_dir)):
        ss = os.path.join(ws_dir, agent, "skill_state.json")
        db = os.path.join(ws_dir, agent, "bridge_memory.sqlite")
        transcript = os.path.join(ws_dir, agent, "transcript.jsonl")
        if not os.path.isfile(ss) or not os.path.isfile(db):
            continue
        try:
            with open(ss) as f:
                state = json.load(f)
            if state.get("memory_sync"):
                agents.append((agent, db, transcript if os.path.isfile(transcript) else None))
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
        return [(rid, ts, "native", memory_type, importance, content)
                for rid, ts, memory_type, importance, content in rows]
    except Exception as e:
        print(f"  ERROR reading {db_path}: {e}")
        return []
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


def _build_ts_lookup(db_path: str, cross_fs: bool) -> dict:
    """Build {text_snippet -> ts} from bridge_memory turns table for timestamp backfill."""
    actual_path = db_path
    tmp_path = None
    if cross_fs:
        tmp_path = f"/tmp/hashi_ts_lookup_{os.getpid()}.sqlite"
        shutil.copy2(db_path, tmp_path)
        actual_path = tmp_path
    lookup = {}
    try:
        conn = sqlite3.connect(actual_path)
        rows = conn.execute("SELECT text, ts FROM turns").fetchall()
        conn.close()
        for text, ts in rows:
            # Use first 120 chars as key
            lookup[text[:120]] = ts
    except Exception:
        pass
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
    return lookup


def read_transcript(transcript_path: str, db_path: str, cross_fs: bool) -> list:
    """
    Read transcript.jsonl and return a list of conversation turns for consolidation.

    Each turn is: (source_id, ts, ts_source, memory_type, importance, content)
    where source_id is -(user_line_number + 1) to avoid colliding with memories table IDs.

    Turns are assembled as: user message + thinking tokens + assistant reply.
    Noise sources (fyi, system, handoff, startup) are dropped.
    """
    # Build timestamp lookup from turns table for entries without ts field
    ts_lookup = _build_ts_lookup(db_path, cross_fs)
    file_mtime = datetime.fromtimestamp(
        os.path.getmtime(transcript_path), tz=timezone.utc
    ).isoformat()

    lines = []
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    lines.append(json.loads(raw))
                except json.JSONDecodeError:
                    lines.append(None)
    except Exception as e:
        print(f"  ERROR reading transcript {transcript_path}: {e}")
        return []

    turns = []
    i = 0
    while i < len(lines):
        entry = lines[i]
        if entry is None:
            i += 1
            continue

        role = entry.get("role", "")
        source = entry.get("source", "text")

        # Skip noise sources entirely
        if source in TRANSCRIPT_NOISE_SOURCES:
            i += 1
            continue

        # Only start a turn on a user message
        if role != "user":
            i += 1
            continue

        user_line_idx = i
        user_text = entry.get("text", "")
        native_ts = entry.get("ts")
        if native_ts:
            user_ts = native_ts
            ts_source = "native"
        else:
            matched_ts = ts_lookup.get(user_text[:120])
            if matched_ts:
                user_ts = matched_ts
                ts_source = "turns_match"
            else:
                user_ts = file_mtime
                ts_source = "mtime_fallback"

        # Collect thinking tokens that follow this user message
        thinking_parts = []
        j = i + 1
        while j < len(lines):
            next_entry = lines[j]
            if next_entry is None:
                j += 1
                continue
            next_role = next_entry.get("role", "")
            next_source = next_entry.get("source", "text")
            if next_role == "thinking" or next_source == "think":
                thinking_parts.append(next_entry.get("text", ""))
                j += 1
            else:
                break

        # Collect the assistant reply
        assistant_text = ""
        if j < len(lines) and lines[j] is not None and lines[j].get("role") == "assistant":
            assistant_text = lines[j].get("text", "")
            j += 1

        # Skip if no meaningful content
        if not user_text.strip() and not assistant_text.strip():
            i = j
            continue

        # Assemble combined content — assistant reply gets priority budget
        # Budget: assistant gets up to 2000 chars, thinking up to 600, user up to 600
        user_snippet = user_text[:600]
        thinking_snippet = ""
        if thinking_parts:
            thinking_snippet = " ".join(thinking_parts)[:600]
        assistant_snippet = assistant_text[:2000]

        parts = []
        parts.append(f"[User ({source})]: {user_snippet}")
        if thinking_snippet:
            parts.append(f"[Thinking]: {thinking_snippet}")
        if assistant_snippet:
            parts.append(f"[Assistant]: {assistant_snippet}")
        content = "\n\n".join(parts)

        # source_id determination:
        # - For scheduler/cron: hash(user_prompt_prefix + assistant_prefix) so that
        #   identical cron prompts with identical replies are deduped, but different
        #   daily results (different assistant text) each get their own record.
        #   Use negative hash range to avoid collision with memories table IDs.
        # - For regular turns: use negative line index (guaranteed unique per file)
        if source in TRANSCRIPT_SCHEDULER_SOURCES:
            key_str = user_text[:200] + "||" + assistant_text[:200]
            h = int(hashlib.md5(key_str.encode()).hexdigest(), 16)
            # Map to a large negative integer range
            source_id = -(h % 2_000_000_000 + 1)
        else:
            source_id = -(user_line_idx + 1)

        turns.append((source_id, user_ts, ts_source, "turn", 1.0, content))
        i = j

    return turns


def consolidate():
    """Main consolidation routine."""
    now = datetime.now(timezone.utc).isoformat()
    print(f"=== Memory Consolidation — {now} ===\n")

    habit_conn = init_habit_evaluation_db(HABIT_EVAL_DB)
    habit_conn.close()

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

        for agent_id, db_path, transcript_path in agents:
            stats["scanned_agents"] += 1
            stats["by_instance"][inst_name]["agents"] += 1

            # --- Phase 1a: memories from bridge_memory.sqlite ---
            memories = read_memories(db_path, instance["cross_fs"])
            # --- Phase 1b: conversation turns from transcript.jsonl ---
            turns = []
            if transcript_path:
                turns = read_transcript(transcript_path, db_path, instance["cross_fs"])

            all_rows = memories + turns
            agent_new = 0

            for row in all_rows:
                source_id, ts, ts_source, mem_type, importance, content = row
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
                            source_ts, ts_source, consolidated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (inst_name, agent_id, source_id, domain,
                         mem_type, importance, content, ts, ts_source, now),
                    )
                    stats["new_inserted"] += 1
                    stats["by_domain"][domain] += 1
                    agent_new += 1
                    existing.add(key)
                except Exception as e:
                    stats["errors"] += 1
                    print(f"  ERROR inserting {key}: {e}")

            print(f"  {agent_id}: {len(memories)} memories + {len(turns)} turns scanned, {agent_new} new")
            stats["by_instance"][inst_name]["new"] += agent_new

        conn.commit()
        print()

    conn.close()

    # Write log entry
    with open(CONSOLIDATION_LOG, "a") as f:
        f.write(json.dumps(stats, ensure_ascii=False) + "\n")

    # Print summary
    print("=" * 50)
    print(f"Scanned: {stats['scanned_agents']} agents, {stats['scanned_memories']} records (memories + turns)")
    print(f"New:     {stats['new_inserted']}")
    print(f"Skipped: {stats['skipped_existing']} existing, {stats['skipped_noise']} noise")
    print(f"Errors:  {stats['errors']}")
    print(f"Domains: {stats['by_domain']}")
    print(f"DB:      {CONSOLIDATED_DB}")
    print(f"Habits:  {HABIT_EVAL_DB}")
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
