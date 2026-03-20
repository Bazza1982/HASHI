# Vector Storage Upgrade Plan
## sqlite-vec + BGE-M3 (Local Linux Only)

**Target Environment**: HASHI2 (Linux, same OS as production)
**Status**: Ready for test installation
**Risk Level**: Low — isolated test environment, no cross-OS dependencies
**Estimated Work**: 4–5 hours

---

## Overview

Replace the current hash-based `LocalEmbeddingEncoder` (256-dim, poor semantic quality) with a proper embedding model (BGE-M3, 1024-dim), and replace JSON-in-TEXT vector storage with `sqlite-vec` native KNN queries.

**Before → After**

| Component | Before | After |
|-----------|--------|-------|
| Encoder | Hash-based, 256-dim | BGE-M3 ONNX INT8, 1024-dim |
| Vector storage | TEXT column (JSON string) | sqlite-vec `vec0` virtual table |
| KNN search | Python loop over all rows | SQL-level KNN, `MATCH` clause |
| Query speed | O(n) | O(log n) approximate |
| Semantic quality | Poor | Excellent (multilingual, Chinese+English) |

---

## Step 0: Prerequisites

All dependencies must be installed in the **same Linux Python environment** that runs Hashi.

```bash
# Identify the Python used by hashi
which python3
python3 --version

# Install dependencies
pip install sqlite-vec
pip install onnxruntime          # CPU-only, no GPU required
pip install transformers tokenizers

# Verify sqlite-vec loads
python3 -c "import sqlite_vec; print('sqlite_vec OK:', sqlite_vec.__version__)"

# Verify onnxruntime
python3 -c "import onnxruntime as ort; print('ort OK:', ort.__version__)"
```

---

## Step 1: Download BGE-M3 ONNX INT8 Model

BGE-M3 must be available locally on the Linux machine. Choose one method:

### Option A — Export from HuggingFace (recommended)

```bash
pip install optimum[exporters]

# Export to ONNX INT8 (~543MB)
optimum-cli export onnx \
  --model BAAI/bge-m3 \
  --task feature-extraction \
  --int8 \
  /opt/hashi/models/bge-m3-int8/
```

### Option B — Copy from existing Windows install (if available)

```bash
# On Windows side:
# C:\Users\thene\.cache\bge-m3-onnx-npu\bge-m3-int8.onnx  (~543MB)

# Copy to HASHI2 Linux machine via scp or shared drive
scp /path/to/bge-m3-int8.onnx hashi2:/opt/hashi/models/bge-m3-int8/model.onnx
```

**Target path on HASHI2**: `/opt/hashi/models/bge-m3-int8/`
(can be any path — will be set in config)

Verify the model works:

```bash
python3 - <<'EOF'
import onnxruntime as ort
import numpy as np
from transformers import AutoTokenizer

MODEL_DIR = "/opt/hashi/models/bge-m3-int8"

tokenizer = AutoTokenizer.from_pretrained("BAAI/bge-m3", trust_remote_code=True)
session = ort.InferenceSession(f"{MODEL_DIR}/model.onnx", providers=["CPUExecutionProvider"])

texts = ["Hello world", "你好世界"]
inputs = tokenizer(texts, padding=True, truncation=True, max_length=512, return_tensors="np")
outputs = session.run(None, {
    "input_ids": inputs["input_ids"].astype(np.int64),
    "attention_mask": inputs["attention_mask"].astype(np.int64),
})
print("Output shape:", outputs[0].shape)   # expect (2, seq_len, 1024)
print("Encoder OK ✅")
EOF
```

---

## Step 2: New BgeM3Encoder Class

Add to `orchestrator/bridge_memory.py`, replacing `LocalEmbeddingEncoder`:

```python
class BgeM3Encoder:
    """
    BGE-M3 ONNX INT8 encoder for semantic embeddings.
    Outputs 1024-dim L2-normalised vectors.
    Falls back to LocalEmbeddingEncoder if model not available.
    """
    DIM = 1024
    DEFAULT_MODEL_DIR = "/opt/hashi/models/bge-m3-int8"

    def __init__(self, model_dir: str = None):
        self._ready = False
        self._fallback = LocalEmbeddingEncoder(dim=256)
        model_dir = model_dir or self.DEFAULT_MODEL_DIR

        try:
            import onnxruntime as ort
            import numpy as np
            from transformers import AutoTokenizer

            self._np = np
            self._tokenizer = AutoTokenizer.from_pretrained(model_dir, trust_remote_code=True)
            self._session = ort.InferenceSession(
                f"{model_dir}/model.onnx",
                providers=["CPUExecutionProvider"],
            )
            self._ready = True
        except Exception as e:
            print(f"[BgeM3Encoder] WARNING: falling back to hash encoder — {e}")

    @property
    def dim(self) -> int:
        return self.DIM if self._ready else self._fallback.dim

    def encode(self, text: str) -> list[float]:
        if not self._ready:
            return self._fallback.encode(text)

        np = self._np
        inputs = self._tokenizer(
            [text], padding=True, truncation=True, max_length=512, return_tensors="np"
        )
        outputs = self._session.run(None, {
            "input_ids": inputs["input_ids"].astype(np.int64),
            "attention_mask": inputs["attention_mask"].astype(np.int64),
        })
        # Mean pool over token dimension
        token_embeddings = outputs[0][0]           # (seq_len, 1024)
        mask = inputs["attention_mask"][0]         # (seq_len,)
        masked = token_embeddings * mask[:, None]
        vec = masked.sum(axis=0) / mask.sum()      # (1024,)

        # L2 normalise
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec = vec / norm
        return vec.tolist()

    @staticmethod
    def cosine(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        return sum(x * y for x, y in zip(a, b))
```

---

## Step 3: Add sqlite-vec to DB Initialisation

In `BridgeMemoryStore._init_db()`, load the extension and create `vec0` virtual tables:

```python
def _connect(self) -> sqlite3.Connection:
    conn = sqlite3.connect(self.db_path)
    conn.row_factory = sqlite3.Row

    # Load sqlite-vec extension
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except Exception as e:
        print(f"[BridgeMemoryStore] WARNING: sqlite_vec not loaded — {e}")

    return conn

def _init_db(self):
    dim = self.encoder.dim   # 1024 for BGE-M3, 256 fallback
    with self._connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")

        # Existing tables (unchanged)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                role TEXT NOT NULL,
                source TEXT NOT NULL,
                text TEXT NOT NULL,
                embedding TEXT NOT NULL      -- keep for backward compat
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                memory_type TEXT NOT NULL,
                source TEXT NOT NULL,
                content TEXT NOT NULL,
                importance REAL DEFAULT 1.0,
                embedding TEXT NOT NULL      -- keep for backward compat
            )
        """)

        # NEW: sqlite-vec virtual tables
        conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0(
                memory_id INTEGER PRIMARY KEY,
                embedding FLOAT[{dim}]
            )
        """)
        conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS turns_vec USING vec0(
                turn_id INTEGER PRIMARY KEY,
                embedding FLOAT[{dim}]
            )
        """)

        # Existing FTS5 (unchanged)
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                content, memory_id UNINDEXED, source,
                tokenize='porter'
            )
        """)
```

---

## Step 4: Update Write Path

In `record_turn()` and `record_memories()` (or wherever embeddings are written), insert into `vec0` table in addition to the TEXT column:

```python
def _insert_memory_vec(self, conn, memory_id: int, embedding: list[float]):
    """Insert or replace vector into sqlite-vec table."""
    try:
        import struct
        blob = struct.pack(f"{len(embedding)}f", *embedding)
        conn.execute(
            "INSERT OR REPLACE INTO memory_vec(memory_id, embedding) VALUES (?, ?)",
            (memory_id, blob),
        )
    except Exception:
        pass  # vec table unavailable, graceful degradation

def _insert_turn_vec(self, conn, turn_id: int, embedding: list[float]):
    try:
        import struct
        blob = struct.pack(f"{len(embedding)}f", *embedding)
        conn.execute(
            "INSERT OR REPLACE INTO turns_vec(turn_id, embedding) VALUES (?, ?)",
            (turn_id, blob),
        )
    except Exception:
        pass
```

Call these right after the `INSERT INTO memories` / `INSERT INTO turns` statements, using `conn.lastrowid`.

---

## Step 5: Update Read Path (KNN Query)

Replace the current Python-loop similarity in `retrieve_memories()`:

```python
# OLD: read all embeddings, compute cosine in Python
rows = conn.execute("SELECT * FROM memories").fetchall()
scored = [(LocalEmbeddingEncoder.cosine(query_vec, json.loads(r["embedding"])), r) for r in rows]

# NEW: use sqlite-vec KNN
import struct

def _vec_blob(embedding: list[float]) -> bytes:
    return struct.pack(f"{len(embedding)}f", *embedding)

def retrieve_memories_vec(self, query: str, limit: int = 20) -> list[dict]:
    query_vec = self.encoder.encode(query)
    blob = _vec_blob(query_vec)

    with self._connect() as conn:
        rows = conn.execute("""
            SELECT m.id, m.ts, m.memory_type, m.source, m.content, m.importance,
                   v.distance
            FROM memory_vec v
            JOIN memories m ON m.id = v.memory_id
            WHERE v.embedding MATCH ?
              AND k = ?
            ORDER BY v.distance
        """, (blob, limit * 2)).fetchall()

    # Recency boost (same as current logic)
    now = datetime.utcnow()
    results = []
    for r in rows:
        age_days = (now - datetime.fromisoformat(r["ts"])).days
        recency_score = 1.0 / (1.0 + age_days * 0.1)
        # distance is L2, convert to similarity: sim ≈ 1 - distance/2
        vec_sim = max(0.0, 1.0 - float(r["distance"]) / 2.0)
        score = vec_sim * 0.7 + recency_score * 0.2 + float(r["importance"]) * 0.1
        results.append((score, dict(r)))

    results.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in results[:limit]]
```

The hybrid mode (FTS5 + vec KNN, then merge) can be added after basic KNN is verified working.

---

## Step 6: Migration Script

Run once on HASHI2 to backfill existing memories with new embeddings:

```python
#!/usr/bin/env python3
"""
migrate_vectors.py — one-time migration of bridge_memory.sqlite
Reads all existing memories/turns, re-encodes with BGE-M3, inserts into vec0 tables.

Usage:
    python3 migrate_vectors.py /path/to/workspace/bridge_memory.sqlite
"""
import sys
import json
import struct
import sqlite3
from pathlib import Path

MODEL_DIR = "/opt/hashi/models/bge-m3-int8"

def load_encoder():
    import onnxruntime as ort
    import numpy as np
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR, trust_remote_code=True)
    session = ort.InferenceSession(f"{MODEL_DIR}/model.onnx", providers=["CPUExecutionProvider"])

    def encode(text: str) -> list[float]:
        inputs = tokenizer([text], padding=True, truncation=True, max_length=512, return_tensors="np")
        out = session.run(None, {
            "input_ids": inputs["input_ids"].astype(np.int64),
            "attention_mask": inputs["attention_mask"].astype(np.int64),
        })
        vec = out[0][0]
        mask = inputs["attention_mask"][0]
        masked = vec * mask[:, None]
        mean_vec = masked.sum(axis=0) / mask.sum()
        norm = float(np.linalg.norm(mean_vec))
        return (mean_vec / norm).tolist() if norm > 0 else mean_vec.tolist()

    return encode

def to_blob(vec):
    return struct.pack(f"{len(vec)}f", *vec)

def migrate(db_path: str):
    import sqlite_vec

    encode = load_encoder()
    dim = 1024

    conn = sqlite3.connect(db_path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    # Ensure vec tables exist
    conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0(memory_id INTEGER PRIMARY KEY, embedding FLOAT[{dim}])")
    conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS turns_vec USING vec0(turn_id INTEGER PRIMARY KEY, embedding FLOAT[{dim}])")
    conn.commit()

    # Migrate memories
    rows = conn.execute("SELECT id, content FROM memories").fetchall()
    print(f"Migrating {len(rows)} memories...")
    for i, (mid, content) in enumerate(rows):
        vec = encode(content)
        conn.execute("INSERT OR REPLACE INTO memory_vec(memory_id, embedding) VALUES (?, ?)", (mid, to_blob(vec)))
        if (i + 1) % 50 == 0:
            conn.commit()
            print(f"  {i+1}/{len(rows)}")
    conn.commit()

    # Migrate turns
    rows = conn.execute("SELECT id, text FROM turns").fetchall()
    print(f"Migrating {len(rows)} turns...")
    for i, (tid, text) in enumerate(rows):
        vec = encode(text)
        conn.execute("INSERT OR REPLACE INTO turns_vec(turn_id, embedding) VALUES (?, ?)", (tid, to_blob(vec)))
        if (i + 1) % 100 == 0:
            conn.commit()
            print(f"  {i+1}/{len(rows)}")
    conn.commit()

    conn.close()
    print("Migration complete ✅")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 migrate_vectors.py /path/to/bridge_memory.sqlite")
        sys.exit(1)
    migrate(sys.argv[1])
```

---

## Testing Checklist (HASHI2)

```
[ ] Step 0: pip install sqlite-vec onnxruntime transformers — no errors
[ ] Step 1: BGE-M3 model downloaded, verify script prints shape (2, seq_len, 1024)
[ ] Step 2: BgeM3Encoder added to bridge_memory.py, unit test encode() returns list of 1024 floats
[ ] Step 3: _init_db() creates memory_vec and turns_vec tables without errors
[ ] Step 4: Write path inserts into vec0 tables — verify with:
            SELECT count(*) FROM memory_vec;
[ ] Step 5: KNN query returns results, distances are reasonable (< 2.0)
[ ] Step 6: migrate_vectors.py runs on test DB, all rows migrated
[ ] End-to-end: agent conversation → retrieve_memories() returns semantically relevant memories
[ ] Fallback test: uninstall sqlite-vec, verify agent still starts (falls back to hash encoder)
```

---

## Files to Modify

| File | Change |
|------|--------|
| `orchestrator/bridge_memory.py` | Replace `LocalEmbeddingEncoder` → `BgeM3Encoder`; update `_connect()`, `_init_db()`, write path, read path |
| `docs/migrate_vectors.py` | New one-time migration script (or place in `tools/`) |

No other files need changes. Agent runtimes, gateway, and all other components are unaffected.

---

## Rollback

If anything goes wrong on HASHI2:

1. `BgeM3Encoder.__init__` has a try/except that auto-falls back to `LocalEmbeddingEncoder` if model or onnxruntime is unavailable.
2. The `embedding TEXT` columns on `memories` and `turns` tables are kept — old JSON embeddings remain intact.
3. To fully revert: `git checkout orchestrator/bridge_memory.py`

---

*Prepared by 小蕾 — 2026-03-20*
