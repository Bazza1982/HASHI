from __future__ import annotations

import hashlib
import inspect
import json
import logging
import math
import os
import re
import sqlite3
import struct
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

from orchestrator.pcm import PCMDocument, load_pcm_document
from tools.token_tracker import estimate_tokens as _estimate_tokens

CURRENT_REQUEST_SEPARATOR = "\n\n--- CURRENT USER REQUEST — AUTHORITATIVE ---\n"

sys_prompt_logger = logging.getLogger("BridgeU.SysPrompt")
memory_logger = logging.getLogger("BridgeU.Memory")
_SYS_PROMPT_LOCKS_GUARD = globals().get("_SYS_PROMPT_LOCKS_GUARD", threading.Lock())
_SYS_PROMPT_LOCKS: dict[str, threading.RLock] = globals().get("_SYS_PROMPT_LOCKS", {})


def _sys_prompt_path_lock(path: Path) -> threading.RLock:
    key = str(Path(path).resolve())
    with _SYS_PROMPT_LOCKS_GUARD:
        return _SYS_PROMPT_LOCKS.setdefault(key, threading.RLock())


def global_sys_prompt_state_path(global_config: Any) -> Path:
    """Return the canonical prompt-slot file for one HASHI instance."""

    bridge_home = getattr(global_config, "bridge_home", None) or getattr(
        global_config,
        "project_root",
        None,
    )
    if not bridge_home:
        raise ValueError("Global system prompts require bridge_home or project_root")
    return Path(bridge_home) / "state" / "global_sys_prompts.json"


class LocalEmbeddingEncoder:
    """Dependency-free hashed embedding encoder for durable local retrieval."""

    def __init__(self, dim: int = 256):
        self.dim = dim
        self.vector_dim = dim
        self.ready = True
        self.error = None

    def encode(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = re.findall(r"[a-zA-Z0-9_]+", (text or "").lower())
        if not tokens:
            return vec
        for tok in tokens:
            idx = hash(tok) % self.dim
            vec[idx] += 1.0
        norm = math.sqrt(sum(v * v for v in vec))
        if norm <= 0.0:
            return vec
        return [v / norm for v in vec]

    @staticmethod
    def cosine(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        return sum(x * y for x, y in zip(a, b))


class BgeM3Encoder:
    """BGE-M3 ONNX encoder with safe fallback to the legacy hash encoder."""

    DIM = 1024
    DEFAULT_MODEL_DIR = Path(os.environ.get("HASHI_BGE_M3_MODEL_DIR") or Path.home() / "hashi_models/bge-m3-int8")
    DEFAULT_TOKENIZER_ID = "BAAI/bge-m3"

    def __init__(self, model_dir: str | Path | None = None, tokenizer_dir: str | Path | None = None):
        self._fallback = LocalEmbeddingEncoder()
        self._ready = False
        self._error: str | None = None
        self._np = None
        self._session = None
        self._tokenizer = None
        self._input_names: set[str] = set()
        self._model_dir = Path(
            model_dir
            or self.DEFAULT_MODEL_DIR
        )
        self._tokenizer_dir = Path(
            tokenizer_dir
            or os.environ.get("HASHI_BGE_M3_TOKENIZER_DIR")
            or self._model_dir
        )
        self._init()

    @property
    def dim(self) -> int:
        return self.DIM if self._ready else self._fallback.dim

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def vector_dim(self) -> int | None:
        return self.DIM if self._ready else None

    def _init(self):
        try:
            import numpy as np
            import onnxruntime as ort
            from transformers import AutoTokenizer
        except Exception as exc:
            self._error = f"dependencies unavailable: {exc}"
            return

        # Support both flat layout (model.onnx) and onnx/ subdirectory layout
        if (self._model_dir / "onnx" / "model.onnx").exists():
            model_path = self._model_dir / "onnx" / "model.onnx"
        else:
            model_path = self._model_dir / "model.onnx"
        if not model_path.exists():
            self._error = f"missing model: {model_path}"
            return

        tokenizer_candidates: list[str] = []
        if self._tokenizer_dir.exists():
            tokenizer_candidates.append(str(self._tokenizer_dir))
        if self._model_dir != self._tokenizer_dir and self._model_dir.exists():
            tokenizer_candidates.append(str(self._model_dir))
        tokenizer_candidates.append(self.DEFAULT_TOKENIZER_ID)

        tokenizer = None
        tokenizer_errors: list[str] = []
        for candidate in tokenizer_candidates:
            try:
                tokenizer = AutoTokenizer.from_pretrained(candidate, trust_remote_code=True)
                break
            except Exception as exc:
                tokenizer_errors.append(f"{candidate}: {exc}")
        if tokenizer is None:
            self._error = "tokenizer load failed: " + " | ".join(tokenizer_errors[:3])
            return

        try:
            session = ort.InferenceSession(
                str(model_path),
                providers=["CPUExecutionProvider"],
            )
        except Exception as exc:
            self._error = f"onnx session init failed: {exc}"
            return

        self._np = np
        self._tokenizer = tokenizer
        self._session = session
        self._input_names = {node.name for node in session.get_inputs()}
        self._ready = True

    def encode(self, text: str) -> list[float]:
        if not self._ready:
            return self._fallback.encode(text)

        np = self._np
        inputs = self._tokenizer(
            [text or ""],
            padding=True,
            truncation=True,
            max_length=512,
            return_tensors="np",
        )
        ort_inputs: dict[str, Any] = {}
        for key, value in inputs.items():
            if key in self._input_names:
                ort_inputs[key] = value.astype(np.int64)
        outputs = self._session.run(None, ort_inputs)
        token_embeddings = outputs[0][0]
        mask = inputs["attention_mask"][0].astype(np.float32)
        mask_sum = float(mask.sum())
        if mask_sum <= 0.0:
            return [0.0] * self.DIM
        masked = token_embeddings * mask[:, None]
        vec = masked.sum(axis=0) / mask_sum
        norm = float(np.linalg.norm(vec))
        if norm > 0.0:
            vec = vec / norm
        return vec.astype(np.float32).tolist()

    @staticmethod
    def cosine(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        return sum(x * y for x, y in zip(a, b))


class BridgeMemoryStore:
    def __init__(self, workspace_dir: Path):
        self.workspace_dir = workspace_dir
        self.db_path = workspace_dir / "bridge_memory.sqlite"
        self.legacy_encoder = LocalEmbeddingEncoder()
        self.encoder = LocalEmbeddingEncoder()  # BGE disabled by default; enable via /memory sync
        self._sqlite_vec_supported: bool | None = None
        self._vec_enabled = False
        self._vec_dim: int | None = None
        self._vec_reason: str | None = None
        try:
            self.recency_half_life_days = max(
                0.01,
                float(os.environ.get("HASHI_MEMORY_RECENCY_HALF_LIFE_DAYS", "30")),
            )
        except (TypeError, ValueError):
            self.recency_half_life_days = 30.0
        self._init_db()
        # ``transcript.jsonl`` is the durable delivery record.  Older HASHI
        # builds could clear the working ``turns`` table while leaving that
        # transcript intact, so seed the canonical completed-exchange ledger
        # from the bounded recent transcript during migration/startup.
        try:
            self.reconcile_recent_transcript()
        except Exception as exc:
            # Recovery must never prevent the Agent from starting.  New
            # exchanges will still enter the canonical ledger directly.
            memory_logger.warning(
                "Recent transcript reconciliation failed: %s: %s",
                type(exc).__name__,
                exc,
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        self._ensure_sqlite_vec(conn)
        return conn

    def _ensure_sqlite_vec(self, conn: sqlite3.Connection) -> bool:
        if self._sqlite_vec_supported is False:
            return False
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            self._sqlite_vec_supported = True
            return True
        except Exception as exc:
            self._sqlite_vec_supported = False
            self._vec_reason = f"sqlite-vec unavailable: {exc}"
            return False

    def _vec_table_exists(self, conn: sqlite3.Connection, name: str) -> bool:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        return bool(row and row["sql"])

    def _vec_table_dim(self, conn: sqlite3.Connection, name: str) -> int | None:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        if not row or not row["sql"]:
            return None
        match = re.search(r"embedding\s+float\[(\d+)\]", row["sql"], re.IGNORECASE)
        if not match:
            return None
        return int(match.group(1))

    def _vector_blob(self, embedding: list[float]) -> bytes:
        return struct.pack(f"{len(embedding)}f", *embedding)

    def _upsert_vec(self, conn: sqlite3.Connection, table: str, key_col: str, row_id: int, embedding: list[float]):
        if not self._vec_enabled or not embedding or self._vec_dim != len(embedding):
            return
        try:
            conn.execute(
                f"INSERT OR REPLACE INTO {table}({key_col}, embedding) VALUES (?, ?)",
                (row_id, self._vector_blob(embedding)),
            )
        except Exception:
            pass

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    role TEXT NOT NULL,
                    source TEXT NOT NULL,
                    text TEXT NOT NULL,
                    embedding TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    memory_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    content TEXT NOT NULL,
                    importance REAL NOT NULL DEFAULT 1.0,
                    embedding TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_exchanges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_turn_id INTEGER,
                    assistant_turn_id INTEGER,
                    user_ts TEXT NOT NULL,
                    assistant_ts TEXT NOT NULL,
                    completed_at REAL NOT NULL,
                    user_source TEXT NOT NULL,
                    assistant_source TEXT NOT NULL,
                    user_text TEXT NOT NULL,
                    assistant_text TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    origin_ref TEXT NOT NULL,
                    UNIQUE(origin, origin_ref)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS conversation_exchanges_time
                ON conversation_exchanges(completed_at, id)
                """
            )
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    content,
                    memory_id UNINDEXED,
                    source,
                    tokenize="porter"
                )
                """
            )
            desired_vec_dim = self.encoder.vector_dim
            if desired_vec_dim and self._sqlite_vec_supported:
                existing_dims = {
                    dim
                    for dim in (
                        self._vec_table_dim(conn, "memory_vec"),
                        self._vec_table_dim(conn, "turns_vec"),
                    )
                    if dim is not None
                }
                if not existing_dims:
                    conn.execute(
                        f"""
                        CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0(
                            memory_id INTEGER PRIMARY KEY,
                            embedding FLOAT[{desired_vec_dim}] distance_metric=cosine
                        )
                        """
                    )
                    conn.execute(
                        f"""
                        CREATE VIRTUAL TABLE IF NOT EXISTS turns_vec USING vec0(
                            turn_id INTEGER PRIMARY KEY,
                            embedding FLOAT[{desired_vec_dim}] distance_metric=cosine
                        )
                        """
                    )
                    self._vec_enabled = True
                    self._vec_dim = desired_vec_dim
                elif existing_dims == {desired_vec_dim}:
                    self._vec_enabled = self._vec_table_exists(conn, "memory_vec") and self._vec_table_exists(conn, "turns_vec")
                    self._vec_dim = desired_vec_dim if self._vec_enabled else None
                else:
                    self._vec_reason = (
                        f"existing sqlite-vec tables use dim {sorted(existing_dims)}, "
                        f"current encoder uses {desired_vec_dim}"
                    )
            elif desired_vec_dim:
                self._vec_reason = self._vec_reason or "sqlite-vec unavailable"
            else:
                self._vec_reason = self.encoder.error or "BGE-M3 unavailable; using legacy hash retrieval"
            conn.commit()

    def _now(self) -> str:
        return datetime.now().isoformat()

    @staticmethod
    def _timestamp_epoch(value: Any) -> float:
        text = str(value or "").strip()
        if not text:
            return 0.0
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.astimezone()
            return parsed.timestamp()
        except (TypeError, ValueError, OSError, OverflowError):
            return 0.0

    def _safe_query(self, query: str) -> str:
        q = (query or "").replace('"', '""').strip()
        if not q:
            return ""
        reserved = {"AND", "OR", "NOT", "NEAR"}
        parts = [
            p
            for p in re.findall(r"[a-zA-Z0-9_]+", q)
            if len(p) > 1 and p.upper() not in reserved
        ]
        if not parts:
            return ""
        return " OR ".join(f'"{p}"' for p in parts[:16])

    def record_turn(self, role: str, source: str, text: str) -> int | None:
        clean = (text or "").strip()
        if not clean:
            return None
        embedding = self.encoder.encode(clean)
        emb = json.dumps(embedding)
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO turns (ts, role, source, text, embedding) VALUES (?, ?, ?, ?, ?)",
                (self._now(), role, source, clean, emb),
            )
            if cur.lastrowid is not None:
                self._upsert_vec(conn, "turns_vec", "turn_id", int(cur.lastrowid), embedding)
            conn.commit()
            return int(cur.lastrowid) if cur.lastrowid is not None else None

    def record_completed_exchange(
        self,
        user_text: str,
        assistant_text: str,
        source: str,
        *,
        assistant_source: str = "",
        user_turn_id: int | None = None,
        assistant_turn_id: int | None = None,
        user_ts: str = "",
        assistant_ts: str = "",
        origin: str = "primary",
        origin_ref: str = "",
    ) -> int | None:
        user_clean = (user_text or "").strip()
        assistant_clean = (assistant_text or "").strip()
        if not user_clean or not assistant_clean:
            return None
        now = self._now()
        with self._connect() as conn:
            if user_turn_id and not user_ts:
                row = conn.execute(
                    "SELECT ts FROM turns WHERE id = ?", (int(user_turn_id),)
                ).fetchone()
                user_ts = str(row["ts"] or "") if row is not None else ""
            if assistant_turn_id and not assistant_ts:
                row = conn.execute(
                    "SELECT ts FROM turns WHERE id = ?", (int(assistant_turn_id),)
                ).fetchone()
                assistant_ts = str(row["ts"] or "") if row is not None else ""
            user_ts = str(user_ts or now)
            assistant_ts = str(assistant_ts or now)
            completed_at = self._timestamp_epoch(assistant_ts)
            if completed_at <= 0:
                completed_at = self._timestamp_epoch(now)
            if not origin_ref:
                origin_ref = hashlib.sha256(
                    (
                        f"{user_ts}\0{assistant_ts}\0{user_clean}\0{assistant_clean}"
                    ).encode("utf-8")
                ).hexdigest()
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO conversation_exchanges(
                    user_turn_id, assistant_turn_id,
                    user_ts, assistant_ts, completed_at,
                    user_source, assistant_source,
                    user_text, assistant_text, origin, origin_ref
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(user_turn_id) if user_turn_id else None,
                    int(assistant_turn_id) if assistant_turn_id else None,
                    user_ts,
                    assistant_ts,
                    completed_at,
                    str(source or "unknown"),
                    str(assistant_source or source or "unknown"),
                    user_clean,
                    assistant_clean,
                    str(origin or "primary"),
                    str(origin_ref),
                ),
            )
            if cur.rowcount:
                exchange_id = int(cur.lastrowid)
            else:
                row = conn.execute(
                    """
                    SELECT id FROM conversation_exchanges
                    WHERE origin = ? AND origin_ref = ?
                    """,
                    (str(origin or "primary"), str(origin_ref)),
                ).fetchone()
                exchange_id = int(row["id"]) if row is not None else None
            conn.commit()
        return exchange_id

    def get_completed_exchanges(
        self,
        limit: int = 10,
        *,
        after_epoch: float | None = None,
    ) -> list[dict[str, Any]]:
        requested_limit = max(1, int(limit or 10))
        cutoff = max(0.0, float(after_epoch or 0.0))
        with self._connect() as conn:
            eligible_rows: list[sqlite3.Row] = []
            offset = 0
            batch_size = max(64, requested_limit)
            while len(eligible_rows) < requested_limit:
                rows = conn.execute(
                    """
                    SELECT id, user_turn_id, assistant_turn_id,
                           user_ts, assistant_ts, completed_at,
                           user_source, assistant_source,
                           user_text, assistant_text, origin, origin_ref
                    FROM conversation_exchanges
                    WHERE completed_at >= ?
                    ORDER BY completed_at DESC, id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (cutoff, batch_size, offset),
                ).fetchall()
                if not rows:
                    break
                eligible_rows.extend(
                    row
                    for row in rows
                    if cutoff <= 0
                    or self._timestamp_epoch(row["user_ts"]) >= cutoff
                )
                offset += len(rows)
                if len(rows) < batch_size:
                    break
            eligible_rows = eligible_rows[:requested_limit]
        ordered_rows = list(reversed(eligible_rows))
        result: list[dict[str, Any]] = []
        for row in ordered_rows:
            item = dict(row)
            turn_ids = [
                int(value)
                for value in (item.get("user_turn_id"), item.get("assistant_turn_id"))
                if value
            ]
            item.update(
                {
                    "kind": "primary_exchange",
                    "exchange_id": int(item["id"]),
                    "turn_ids": tuple(turn_ids),
                    "completed_at": float(item.get("completed_at") or 0),
                    "sequence": int(item["id"]),
                    "source": str(item.get("user_source") or "unknown"),
                    "rows": tuple(
                        row_value
                        for row_value in (
                            {
                                "id": item.get("user_turn_id") or 0,
                                "ts": item.get("user_ts") or "",
                                "role": "user",
                                "source": item.get("user_source") or "unknown",
                                "text": item.get("user_text") or "",
                            },
                            {
                                "id": item.get("assistant_turn_id") or 0,
                                "ts": item.get("assistant_ts") or "",
                                "role": "assistant",
                                "source": item.get("assistant_source") or "unknown",
                                "text": item.get("assistant_text") or "",
                            },
                        )
                    ),
                    "receipt_entries": [],
                }
            )
            result.append(item)
        return result

    def reconcile_recent_transcript(self, max_exchanges: int = 15) -> int:
        """Backfill the canonical timeline from bounded delivered transcript rows.

        This is a migration/recovery path, not a second prompt source.  The
        ledger is intentionally independent from the mutable ``turns`` cache.
        Clearing that cache must not erase the immediate conversation timeline
        supplied to the next backend request.
        """

        candidates = [
            path
            for path in (
                self.workspace_dir / "recent_context.jsonl",
                self.workspace_dir / "transcript.jsonl",
            )
            if path.is_file()
        ]
        if not candidates:
            return 0
        candidates.sort(
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        raw_entries: list[dict[str, str]] = []
        for transcript_path in candidates:
            try:
                lines = transcript_path.read_text(
                    encoding="utf-8",
                    errors="replace",
                ).splitlines()
            except OSError:
                continue
            candidate_entries: list[dict[str, str]] = []
            for line in lines:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                role = str(entry.get("role") or "").lower()
                source = str(entry.get("source") or "")
                text = str(entry.get("text") or "").strip()
                if role not in {"user", "assistant"} or not text:
                    continue
                if source in {"startup", "system", "think", "handoff"}:
                    continue
                candidate_entries.append(
                    {
                        "role": role,
                        "source": source or "unknown",
                        "text": text,
                        "ts": str(entry.get("ts") or ""),
                    }
                )
            if candidate_entries:
                raw_entries = candidate_entries
                break
        if not raw_entries:
            return 0

        rounds: list[list[dict[str, str]]] = []
        current: list[dict[str, str]] = []
        for entry in raw_entries:
            if entry["role"] == "user" and current:
                rounds.append(current)
                current = [entry]
            else:
                current.append(entry)
        if current:
            rounds.append(current)
        rounds = rounds[-max(1, int(max_exchanges or 15)) :]

        imported = 0
        for round_entries in rounds:
            users = [entry for entry in round_entries if entry["role"] == "user"]
            assistants = [entry for entry in round_entries if entry["role"] == "assistant"]
            if not users or not assistants:
                continue
            user = users[-1]
            assistant = assistants[-1]
            origin_ref = hashlib.sha256(
                (
                    f"{user.get('ts', '')}\0{assistant.get('ts', '')}\0"
                    f"{user['text']}\0{assistant['text']}"
                ).encode("utf-8")
            ).hexdigest()
            completed_at = self._timestamp_epoch(assistant.get("ts"))
            with self._connect() as conn:
                duplicate_rows = conn.execute(
                    """
                    SELECT assistant_ts FROM conversation_exchanges
                    WHERE user_text = ? AND assistant_text = ?
                    ORDER BY completed_at DESC, id DESC
                    LIMIT 4
                    """,
                    (user["text"], assistant["text"]),
                ).fetchall()
                already_recorded = any(
                    abs(
                        self._timestamp_epoch(row["assistant_ts"])
                        - completed_at
                    )
                    <= 30
                    for row in duplicate_rows
                    if completed_at > 0
                    and self._timestamp_epoch(row["assistant_ts"]) > 0
                )
                if not already_recorded:
                    already_recorded = (
                        conn.execute(
                            """
                            SELECT 1 FROM conversation_exchanges
                            WHERE origin = 'transcript' AND origin_ref = ?
                            """,
                            (origin_ref,),
                        ).fetchone()
                        is not None
                    )
            if already_recorded:
                continue
            before = self.record_completed_exchange(
                user["text"],
                assistant["text"],
                user["source"],
                assistant_source=assistant["source"],
                user_ts=user.get("ts", ""),
                assistant_ts=assistant.get("ts", ""),
                origin="transcript",
                origin_ref=origin_ref,
            )
            if before is not None:
                imported += 1
        return imported

    def record_memory(self, memory_type: str, source: str, content: str, importance: float = 1.0) -> int | None:
        clean = (content or "").strip()
        if not clean:
            return None
        embedding = self.encoder.encode(clean)
        emb = json.dumps(embedding)
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO memories (ts, memory_type, source, content, importance, embedding)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (self._now(), memory_type, source, clean, float(importance), emb),
            )
            memory_id = int(cur.lastrowid) if cur.lastrowid is not None else None
            if memory_id is not None:
                conn.execute(
                    "INSERT INTO memory_fts (content, memory_id, source) VALUES (?, ?, ?)",
                    (clean, memory_id, source),
                )
                self._upsert_vec(conn, "memory_vec", "memory_id", memory_id, embedding)
            conn.commit()
        return memory_id

    def record_exchange(self, user_text: str, assistant_text: str, source: str):
        user_clean = (user_text or "").strip()
        assistant_clean = (assistant_text or "").strip()
        if not user_clean or not assistant_clean:
            return
        episode = f"User: {user_clean}\nAssistant: {assistant_clean}"
        self.record_memory("episodic", source, episode, importance=1.0)

    def get_recent_turns(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT role, source, text, ts
                FROM turns
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def get_last_user_turn_ts(self) -> str | None:
        """Return the ISO timestamp of the most recent user turn, or None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT ts FROM turns WHERE role = 'user' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return row["ts"] if row else None

    def retrieve_memories(
        self,
        query: str,
        limit: int = 6,
        *,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieve memories with separately observable relevance components."""

        safe_query = self._safe_query(query)
        q_vec = self.encoder.encode(query or "")
        legacy_q_vec = self.legacy_encoder.encode(query or "")
        candidates: dict[int, dict[str, Any]] = {}

        with self._connect() as conn:
            if self._vec_enabled and query.strip():
                try:
                    rows = conn.execute(
                        """
                        SELECT m.id, m.ts, m.memory_type, m.source, m.content, m.importance,
                               m.embedding, v.distance
                        FROM memory_vec v
                        JOIN memories m ON m.id = v.memory_id
                        WHERE v.embedding MATCH ?
                          AND k = ?
                        ORDER BY distance
                        LIMIT ?
                        """,
                        (self._vector_blob(q_vec), max(limit * 6, 24), max(limit * 6, 24)),
                    ).fetchall()
                    for row in rows:
                        item = dict(row)
                        item["_vector_match"] = True
                        candidates[row["id"]] = item
                except Exception:
                    pass
            if safe_query:
                try:
                    rows = conn.execute(
                        """
                        SELECT m.id, m.ts, m.memory_type, m.source, m.content, m.importance, m.embedding
                        FROM memory_fts f
                        JOIN memories m ON m.id = f.memory_id
                        WHERE memory_fts MATCH ?
                        LIMIT 40
                        """,
                        (safe_query,),
                    ).fetchall()
                    for row in rows:
                        existing = candidates.get(row["id"], {})
                        existing.update(dict(row))
                        existing["_text_match"] = True
                        candidates[row["id"]] = existing
                except sqlite3.OperationalError:
                    pass

            recent_rows = conn.execute(
                """
                SELECT id, ts, memory_type, source, content, importance, embedding
                FROM memories
                ORDER BY id DESC
                LIMIT 60
                """
            ).fetchall()
            for row in recent_rows:
                candidates.setdefault(row["id"], dict(row))

        scoring_now = now or datetime.now(timezone.utc)
        if scoring_now.tzinfo is None:
            scoring_now = scoring_now.replace(tzinfo=timezone.utc)
        now_epoch = scoring_now.timestamp()
        half_life_seconds = self.recency_half_life_days * 86400.0
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in candidates.values():
            sim = 0.0
            if row.get("distance") is not None:
                try:
                    sim = max(0.0, 1.0 - float(row["distance"]))
                except Exception:
                    sim = 0.0
            elif row.get("embedding"):
                try:
                    emb = json.loads(row["embedding"])
                except Exception:
                    emb = []
                if len(emb) == len(q_vec):
                    sim = self.encoder.cosine(q_vec, emb)
                elif len(emb) == len(legacy_q_vec):
                    sim = self.legacy_encoder.cosine(legacy_q_vec, emb)
            vector_score = max(0.0, min(1.0, float(sim)))
            text_score = 1.0 if row.pop("_text_match", False) else 0.0
            row.pop("_vector_match", None)
            importance_score = max(
                0.0, min(1.0, float(row.get("importance", 1.0)))
            )
            memory_epoch = self._timestamp_epoch(row.get("ts"))
            age_seconds = max(0.0, now_epoch - memory_epoch) if memory_epoch else float("inf")
            recency_score = (
                math.exp(-math.log(2.0) * age_seconds / half_life_seconds)
                if math.isfinite(age_seconds)
                else 0.0
            )
            score = (
                0.55 * vector_score
                + 0.20 * text_score
                + 0.15 * importance_score
                + 0.10 * recency_score
            )
            row["vector_score"] = vector_score
            row["text_score"] = text_score
            row["importance_score"] = importance_score
            row["recency_score"] = recency_score
            row["age_seconds"] = age_seconds
            row["recency_half_life_days"] = self.recency_half_life_days
            row["score"] = score
            scored.append((score, row))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [row for _, row in scored[:limit]]

    def get_stats(self) -> dict[str, int]:
        with self._connect() as conn:
            turns = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
            memories = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        return {"turns": int(turns), "memories": int(memories)}

    def clear_turns(self) -> int:
        """Delete working turns without destroying completed exchange history."""
        with self._connect() as conn:
            deleted = conn.execute("DELETE FROM turns").rowcount
            try:
                conn.execute("DELETE FROM turns_vec")
            except Exception:
                pass
            conn.commit()
        return int(deleted)

    def clear_all(self) -> dict[str, int]:
        """Wipe all stored turns and memories. Keeps the database file and schema intact."""
        with self._connect() as conn:
            deleted_turns = conn.execute("DELETE FROM turns").rowcount
            deleted_memories = conn.execute("DELETE FROM memories").rowcount
            conn.execute("DELETE FROM memory_fts")
            conn.execute("DELETE FROM conversation_exchanges")
            try:
                conn.execute("DELETE FROM memory_vec")
                conn.execute("DELETE FROM turns_vec")
            except Exception:
                pass
            conn.commit()
        return {"deleted_turns": int(deleted_turns), "deleted_memories": int(deleted_memories)}

    def get_vector_status(self) -> dict[str, Any]:
        status: dict[str, Any] = {
            "db_path": str(self.db_path),
            "encoder_ready": bool(self.encoder.ready),
            "encoder_dim": self.encoder.vector_dim,
            "encoder_error": self.encoder.error,
            "sqlite_vec_supported": bool(self._sqlite_vec_supported),
            "vec_enabled": bool(self._vec_enabled),
            "vec_dim": self._vec_dim,
            "vec_reason": self._vec_reason,
            "tables": {},
            "counts": {
                "memories": 0,
                "memory_vec": 0,
                "turns": 0,
                "turns_vec": 0,
            },
            "coverage": {
                "memories": 0.0,
                "turns": 0.0,
            },
            "overall_status": "fallback_active",
        }
        with self._connect() as conn:
            for table_name in ("memory_vec", "turns_vec"):
                exists = self._vec_table_exists(conn, table_name)
                status["tables"][table_name] = {
                    "exists": exists,
                    "dim": self._vec_table_dim(conn, table_name) if exists else None,
                }

            status["counts"]["memories"] = int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
            status["counts"]["turns"] = int(conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0])

            if status["tables"]["memory_vec"]["exists"]:
                try:
                    status["counts"]["memory_vec"] = int(conn.execute("SELECT COUNT(*) FROM memory_vec").fetchone()[0])
                except Exception:
                    status["counts"]["memory_vec"] = 0
            if status["tables"]["turns_vec"]["exists"]:
                try:
                    status["counts"]["turns_vec"] = int(conn.execute("SELECT COUNT(*) FROM turns_vec").fetchone()[0])
                except Exception:
                    status["counts"]["turns_vec"] = 0

        memories_total = status["counts"]["memories"]
        turns_total = status["counts"]["turns"]
        status["coverage"]["memories"] = (
            status["counts"]["memory_vec"] / memories_total if memories_total else 1.0
        )
        status["coverage"]["turns"] = (
            status["counts"]["turns_vec"] / turns_total if turns_total else 1.0
        )

        if status["vec_enabled"]:
            fully_backfilled = (
                status["coverage"]["memories"] >= 0.999
                and status["coverage"]["turns"] >= 0.999
            )
            status["overall_status"] = "fully_upgraded" if fully_backfilled else "partially_upgraded"
        elif status["encoder_ready"] and status["sqlite_vec_supported"]:
            status["overall_status"] = "upgrade_available_not_enabled"

        return status


class SysPromptManager:
    """Manage ten local or instance-shared additional system prompt slots."""

    SLOTS: ClassVar[list[str]] = [str(i) for i in range(1, 11)]

    def __init__(
        self,
        workspace_dir: Path | None = None,
        *,
        state_path: Path | None = None,
        shared: bool = False,
        scope: str = "local",
    ):
        if state_path is None:
            if workspace_dir is None:
                raise ValueError("workspace_dir or state_path is required")
            state_path = Path(workspace_dir) / "sys_prompts.json"
        self.state_path = Path(state_path)
        self.shared = bool(shared)
        self.scope = "global" if scope == "global" else "local"
        self._lock = _sys_prompt_path_lock(self.state_path)
        with self._lock:
            self._data: dict = self._load() or self._empty_slots()

    @classmethod
    def for_instance(cls, global_config: Any) -> SysPromptManager:
        return cls(
            state_path=global_sys_prompt_state_path(global_config),
            shared=True,
            scope="global",
        )

    @classmethod
    def _empty_slots(cls) -> dict[str, dict[str, object]]:
        return {slot: {"text": "", "active": False} for slot in cls.SLOTS}

    @classmethod
    def _normalize(cls, payload: object) -> dict[str, dict[str, object]]:
        if not isinstance(payload, dict):
            raise TypeError("system prompt state must be a JSON object")
        normalized = cls._empty_slots()
        for slot in cls.SLOTS:
            raw = payload.get(slot)
            if not isinstance(raw, dict):
                continue
            text = raw.get("text", "")
            normalized[slot] = {
                "text": text if isinstance(text, str) else str(text or ""),
                "active": bool(raw.get("active", False)),
            }
        return normalized

    def _load(self) -> dict[str, dict[str, object]] | None:
        if not self.state_path.exists():
            return self._empty_slots()
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            return self._normalize(payload)
        except Exception as exc:  # noqa: BLE001 - malformed state must not stop an Agent
            sys_prompt_logger.error(
                "Could not load %s system prompt state from %s: %s",
                self.scope,
                self.state_path,
                exc,
            )
            return None

    def _refresh_locked(self) -> None:
        if not self.shared:
            return
        loaded = self._load()
        if loaded is not None:
            self._data = loaded

    def _save_locked(self) -> None:
        self._data = self._normalize(self._data)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(
            f".{self.state_path.name}.tmp-{os.getpid()}-{threading.get_ident()}"
        )
        try:
            temporary.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.state_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _save(self) -> None:
        with self._lock:
            self._save_locked()

    def _slot(self, n: str) -> dict:
        with self._lock:
            self._refresh_locked()
            self._data.setdefault(n, {"text": "", "active": False})
            return self._data[n]

    def get_slot(self, n: str) -> dict[str, object]:
        with self._lock:
            self._refresh_locked()
            slot = self._data.setdefault(n, {"text": "", "active": False})
            return {"text": str(slot.get("text") or ""), "active": bool(slot.get("active"))}

    def display_all(self) -> str:
        lines = ["*System Prompt Slots:*"]
        for item in self.list_slots():
            slot = str(item["slot"])
            s = item
            status = "ON" if s["active"] else "off"
            text = str(s["text"])
            preview = (text[:60] + "…") if len(text) > 60 else text
            lines.append(f"[{slot}] {status} | {preview or '(empty)'}")
        return "\n".join(lines)

    def display_slot(self, n: str) -> str:
        s = self.get_slot(n)
        status = "ON" if s["active"] else "off"
        text = s["text"] or "(empty)"
        return f"Slot {n} [{status}]:\n{text}"

    def activate(self, n: str) -> str:
        with self._lock:
            self._refresh_locked()
            if not self._data.setdefault(n, {"text": "", "active": False})["text"]:
                return f"Slot {n} is empty — save a message first."
            self._data[n]["active"] = True
            self._save_locked()
        prefix = "Global slot" if self.scope == "global" else "Slot"
        return f"{prefix} {n} activated."

    def deactivate(self, n: str) -> str:
        with self._lock:
            self._refresh_locked()
            self._data.setdefault(n, {"text": "", "active": False})["active"] = False
            self._save_locked()
        prefix = "Global slot" if self.scope == "global" else "Slot"
        return f"{prefix} {n} deactivated."

    def save(self, n: str, text: str) -> str:
        with self._lock:
            self._refresh_locked()
            self._data[n] = {"text": text, "active": False}
            self._save_locked()
        if self.scope == "global":
            return f"Global slot {n} saved (inactive). Use /sys global {n} on to activate."
        return f"Slot {n} saved (inactive). Use /sys {n} on to activate."

    def replace(self, n: str, text: str) -> str:
        with self._lock:
            self._refresh_locked()
            was_active = self._data.setdefault(n, {"text": "", "active": False}).get("active", False)
            self._data[n] = {"text": text, "active": was_active}
            self._save_locked()
        prefix = "Global slot" if self.scope == "global" else "Slot"
        return f"{prefix} {n} updated."

    def delete(self, n: str) -> str:
        with self._lock:
            self._refresh_locked()
            self._data[n] = {"text": "", "active": False}
            self._save_locked()
        prefix = "Global slot" if self.scope == "global" else "Slot"
        return f"{prefix} {n} cleared."

    def get_active_texts(self) -> list[str]:
        return [str(item["text"]) for item in self.get_active_entries()]

    def get_active_entries(self) -> list[dict[str, object]]:
        return [item for item in self.list_slots() if item["active"] and item["text"]]

    def list_slots(self) -> list[dict[str, object]]:
        """Return every stable SYS slot without exposing mutable state."""
        with self._lock:
            self._refresh_locked()
            return [
                {
                    "slot": slot,
                    "active": bool(self._data.get(slot, {}).get("active")),
                    "text": str(self._data.get(slot, {}).get("text") or ""),
                }
                for slot in self.SLOTS
            ]


class BridgeContextAssembler:
    """Assemble backend-neutral PCM with explicit authority metadata."""

    PROMPT_BUDGETS: ClassVar[dict[str, int]] = {
        "codex-cli": 24000,
        "gemini-cli": 24000,
        "claude-cli": 50000,
        "openrouter-api": 35000,
        "ollama-api": 30000,
    }
    MAX_RECENT_EXCHANGES: ClassVar[int] = 10
    AUTHORITY_RANKS: ClassVar[dict[str, int]] = {
        "permanent_system": 900,
        "global_system": 800,
        "local_system": 700,
        "current_user": 600,
        "persona": 500,
        "memory": 400,
        "history": 300,
        "runtime_context": 200,
    }

    def __init__(
        self,
        memory_store: BridgeMemoryStore,
        system_md: Path | None,
        active_skill_provider=None,
        sys_prompt_manager=None,
        global_sys_prompt_manager=None,
        skill_catalog_provider=None,
        tool_catalog_provider=None,
    ):
        self.memory_store = memory_store
        self.system_md = system_md
        self.active_skill_provider = active_skill_provider
        self.sys_prompt_manager = sys_prompt_manager
        self.global_sys_prompt_manager = global_sys_prompt_manager
        self.skill_catalog_provider = skill_catalog_provider
        self.tool_catalog_provider = tool_catalog_provider
        self.turns_injection_enabled = True
        self.saved_memory_injection_enabled = False

    @property
    def memory_injection_enabled(self) -> bool:
        return self.turns_injection_enabled and self.saved_memory_injection_enabled

    @memory_injection_enabled.setter
    def memory_injection_enabled(self, enabled: bool) -> None:
        value = bool(enabled)
        self.turns_injection_enabled = value
        self.saved_memory_injection_enabled = value

    def _load_pcm(self) -> PCMDocument | None:
        if not self.system_md:
            return None
        return load_pcm_document(self.system_md, workspace_dir=self.system_md.parent)

    def _load_system_prompt(self) -> str:
        document = self._load_pcm()
        return document.system if document else ""

    @staticmethod
    def _catalogue_lines(provider) -> list[str]:
        if not callable(provider):
            return []
        try:
            raw_items = list(provider() or [])
        except Exception:
            return []
        lines: list[str] = []
        for item in raw_items:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("id") or "").strip()
                description = str(
                    item.get("description") or item.get("summary") or ""
                ).strip()
            elif isinstance(item, (tuple, list)) and item:
                name = str(item[0] or "").strip()
                description = str(item[1] or "").strip() if len(item) > 1 else ""
            else:
                name, description = str(item or "").strip(), ""
            if name:
                lines.append(f"- {name}" + (f": {description}" if description else ""))
        return lines

    def _build_time_fyi(self) -> str:
        """Build accurate local date/time information for one external turn."""

        now = datetime.now().astimezone()
        zone_name = now.tzname() or "local"
        offset = now.strftime("%z")
        offset = f"{offset[:3]}:{offset[3:]}" if len(offset) == 5 else offset
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        base = f"Current local time: {now_str} {zone_name} (UTC{offset})."
        last_ts = self.memory_store.get_last_user_turn_ts()
        if not last_ts:
            return base
        try:
            last_dt = datetime.fromisoformat(last_ts)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=now.tzinfo)
            else:
                last_dt = last_dt.astimezone(now.tzinfo)
            total_seconds = max(0, int((now - last_dt).total_seconds()))
            if total_seconds < 60:
                gap = f"{total_seconds}s"
            elif total_seconds < 3600:
                gap = f"{total_seconds // 60}m"
            elif total_seconds < 86400:
                gap = f"{total_seconds // 3600}h {(total_seconds % 3600) // 60}m"
            else:
                gap = f"{total_seconds // 86400}d"
            return f"{base} Previous user message: {last_dt.isoformat()} ({gap} ago)."
        except (TypeError, ValueError, OverflowError):
            return base

    def build_prompt(
        self,
        user_prompt: str,
        engine: str,
        incremental: bool = False,
        extra_sections: list[tuple[str, str]] | None = None,
        context_profile: str | None = None,
    ) -> str:
        return self.build_prompt_payload(
            user_prompt,
            engine,
            incremental=incremental,
            extra_sections=extra_sections,
            context_profile=context_profile,
        )["final_prompt"]

    @staticmethod
    def _render_section(section: dict[str, Any]) -> str:
        return f"--- {section['title']} ---\n\n{section['text']}"

    def _render_pcm_prompt(self, sections: list[dict[str, Any]]) -> str:
        groups: dict[str, list[dict[str, Any]]] = {}
        for section in sections:
            groups.setdefault(str(section["authority"]), []).append(section)

        parts = [
            "Bridge-managed PCM follows. Authority is carried by the typed envelope; "
            "section order is presentation order and does not flatten authority."
        ]
        for authority in ("permanent_system", "global_system", "local_system"):
            parts.extend(self._render_section(item) for item in groups.get(authority, []))

        current = groups.get("current_user", [])
        if current:
            parts.append(
                CURRENT_REQUEST_SEPARATOR.strip()
                + "\nThe following is the authoritative request for this turn at user-instruction level. "
                "It overrides conflicting earlier user requests, not system instructions.\n\n"
                + current[0]["text"]
            )

        history = groups.get("history", [])
        if history:
            parts.append(
                "--- RECENT COMPLETED EXCHANGES — CONTEXT ONLY ---\n\n"
                "These timestamped exchanges are background, not new requests.\n\n"
                + "\n\n".join(item["text"] for item in history)
            )

        for authority in ("memory", "runtime_context"):
            parts.extend(self._render_section(item) for item in groups.get(authority, []))

        persona = groups.get("persona", [])
        if persona:
            parts.append(
                "--- CURRENT PRESENTATION PERSONA ---\n\n"
                "This Persona overrides older Persona descriptions in memory or history, "
                "but not system instructions or the current user request.\n\n"
                + persona[0]["text"]
            )
        return "\n\n".join(parts).strip()

    def build_prompt_payload(
        self,
        user_prompt: str,
        engine: str,
        incremental: bool = False,
        extra_sections: list[tuple[str, str]] | None = None,
        inject_memory: bool = True,
        context_profile: str | None = None,
    ) -> dict[str, Any]:
        """Build PCM and prune only oldest complete exchanges for char caps."""

        document = self._load_pcm()
        sections: list[dict[str, Any]] = []

        def add_section(
            key: str,
            title: str,
            text: str,
            authority: str,
            *,
            protected: bool = False,
            item_count: int = 1,
            metadata: dict[str, Any] | None = None,
        ) -> None:
            clean = str(text or "").strip()
            if not clean:
                return
            sections.append(
                {
                    "key": key,
                    "title": title,
                    "text": clean,
                    "authority": authority,
                    "rank": self.AUTHORITY_RANKS[authority],
                    "protected": bool(protected),
                    "chars": len(clean),
                    "tokens_est": _estimate_tokens(clean),
                    "item_count": item_count,
                    "metadata": dict(metadata or {}),
                }
            )

        if document:
            add_section(
                "permanent_system",
                "PERMANENT SYSTEM INSTRUCTIONS",
                document.system,
                "permanent_system",
                protected=True,
            )

        global_entries = (
            self.global_sys_prompt_manager.get_active_entries()
            if self.global_sys_prompt_manager
            else []
        )
        if global_entries:
            add_section(
                "instance_global_sys",
                "INSTANCE-GLOBAL /sys",
                "\n\n".join(
                    f"[Global /sys slot {item['slot']}]\n{item['text']}"
                    for item in global_entries
                ),
                "global_system",
                protected=True,
                item_count=len(global_entries),
            )
        local_entries = (
            self.sys_prompt_manager.get_active_texts() if self.sys_prompt_manager else []
        )
        if local_entries:
            add_section(
                "agent_local_sys",
                "AGENT-LOCAL /sys",
                "\n\n".join(local_entries),
                "local_system",
                protected=True,
                item_count=len(local_entries),
            )

        add_section(
            "current_user_request",
            "CURRENT USER REQUEST",
            user_prompt,
            "current_user",
            protected=True,
        )

        managed_history_title = ""
        managed_history = False
        try:
            from orchestrator.context_compaction import (
                MANAGED_HISTORY_TITLE,
                managed_history_present,
            )

            managed_history_title = MANAGED_HISTORY_TITLE
            managed_history = managed_history_present(extra_sections or [])
        except Exception:
            pass

        should_inject_history = (
            not incremental
            and inject_memory
            and self.turns_injection_enabled
            and not managed_history
        )
        exchanges = []
        if should_inject_history:
            getter = self.memory_store.get_completed_exchanges
            kwargs: dict[str, Any] = {"limit": self.MAX_RECENT_EXCHANGES}
            try:
                if "after_epoch" in inspect.signature(getter).parameters:
                    from orchestrator.fresh_context import workspace_cutoff_epoch

                    workspace = getattr(self.memory_store, "workspace_dir", None)
                    if workspace is not None:
                        kwargs["after_epoch"] = workspace_cutoff_epoch(workspace)
            except (TypeError, ValueError):
                pass
            exchanges = getter(**kwargs)
        for exchange in exchanges:
            sequence = int(exchange.get("sequence") or exchange.get("exchange_id") or 0)
            user_ts = str(exchange.get("user_ts") or "unknown-time")
            assistant_ts = str(exchange.get("assistant_ts") or "unknown-time")
            add_section(
                f"recent_exchange:{sequence}",
                "RECENT COMPLETED EXCHANGE",
                (
                    f"Exchange sequence={sequence}; user_ts={user_ts}; assistant_ts={assistant_ts}\n"
                    f"USER: {exchange.get('user_text', '')}\n"
                    f"ASSISTANT: {exchange.get('assistant_text', '')}"
                ),
                "history",
                metadata={"sequence": sequence, "exchange_id": exchange.get("exchange_id")},
            )

        if document and document.memory:
            add_section(
                "permanent_memory",
                "LONG-TERM MEMORY FROM agent.md",
                document.memory,
                "memory",
                protected=True,
            )

        inject_search = (
            not incremental and inject_memory and self.saved_memory_injection_enabled
        )
        retrieved = (
            self.memory_store.retrieve_memories(user_prompt, limit=6)
            if inject_search
            else []
        )
        if retrieved:
            add_section(
                "relevant_long_term_memory",
                "OPTIONAL SEARCHED LONG-TERM MEMORY",
                "\n\n".join(
                    f"[{item['memory_type']}/{item['source']}] {item['content']}"
                    for item in retrieved
                ),
                "memory",
                item_count=len(retrieved),
            )

        time_fyi = self._build_time_fyi()
        add_section(
            "time",
            "DATE AND TIME",
            time_fyi,
            "runtime_context",
            protected=True,
        )

        active_runtime = []
        if callable(self.active_skill_provider):
            try:
                active_runtime = list(self.active_skill_provider() or [])
            except Exception:
                active_runtime = []
        if active_runtime:
            add_section(
                "active_runtime_instructions",
                "ACTIVE RUNTIME INSTRUCTIONS",
                "\n\n".join(
                    f"## [{item[0]}] {item[1]}\n{item[2]}" for item in active_runtime
                ),
                "runtime_context",
                item_count=len(active_runtime),
            )

        for title, body in extra_sections or []:
            if not title or not body:
                continue
            authority = "history" if managed_history_title and title == managed_history_title else "runtime_context"
            add_section(
                f"extra:{str(title).lower().replace(' ', '_')}",
                str(title),
                str(body),
                authority,
                item_count=1,
            )

        skill_lines = self._catalogue_lines(self.skill_catalog_provider)
        if skill_lines:
            add_section(
                "skills_catalogue",
                "AVAILABLE HASHI SKILLS",
                "\n".join(skill_lines),
                "runtime_context",
                item_count=len(skill_lines),
            )
        tool_lines = self._catalogue_lines(self.tool_catalog_provider)
        if tool_lines:
            add_section(
                "tools_catalogue",
                "AVAILABLE HASHI TOOLS",
                "\n".join(tool_lines),
                "runtime_context",
                item_count=len(tool_lines),
            )

        if document:
            add_section(
                "persona",
                "CURRENT PRESENTATION PERSONA",
                document.persona,
                "persona",
                protected=True,
            )

        unbudgeted_sections = list(sections)
        final_prompt_unbudgeted = self._render_pcm_prompt(unbudgeted_sections)
        unbudgeted_context_text = "\n\n".join(
            section["text"]
            for section in unbudgeted_sections
            if section["authority"] != "current_user"
        )
        limit = self.PROMPT_BUDGETS.get(engine, 30000)
        omitted: list[dict[str, Any]] = []
        if engine != "her-v2":
            while len(self._render_pcm_prompt(sections)) > limit:
                history_index = next(
                    (
                        index
                        for index, section in enumerate(sections)
                        if section["authority"] == "history"
                        and section["key"].startswith("recent_exchange:")
                    ),
                    None,
                )
                if history_index is None:
                    break
                removed = sections.pop(history_index)
                omitted.append(
                    {
                        "key": removed["key"],
                        "sequence": removed["metadata"].get("sequence"),
                        "reason": "assembled_request_character_cap",
                    }
                )
        final_prompt = self._render_pcm_prompt(sections)
        if omitted:
            memory_logger.warning(
                "PCM history omission: engine=%s omitted=%s retained=%s limit_chars=%s",
                engine,
                len(omitted),
                sum(1 for section in sections if section["authority"] == "history"),
                limit,
            )

        envelope_sections = [
            {
                "key": section["key"],
                "authority": section["authority"],
                "rank": section["rank"],
                "protected": section["protected"],
                "content_sha256": hashlib.sha256(
                    section["text"].encode("utf-8")
                ).hexdigest(),
                "metadata": section["metadata"],
            }
            for section in sections
        ]
        context_text = "\n\n".join(
            section["text"]
            for section in sections
            if section["authority"] != "current_user"
        )
        return {
            "final_prompt": final_prompt,
            "envelope": {
                "version": 1,
                "current_request_key": "current_user_request",
                "sections": envelope_sections,
            },
            "audit": {
                "incremental": incremental,
                "context_profile": context_profile,
                "budget_limit_chars": limit,
                "budget_applied": bool(omitted),
                "budget_unresolved": engine != "her-v2" and len(final_prompt) > limit,
                "context_chars_before_budget": len(unbudgeted_context_text),
                "final_prompt_chars_before_budget": len(final_prompt_unbudgeted),
                "final_prompt_chars_after_budget": len(final_prompt),
                "time_fyi_chars": len(time_fyi),
                "context_fingerprint": hashlib.sha1(
                    context_text.encode("utf-8")
                ).hexdigest()[:16],
                "history_requested": len(exchanges),
                "history_included": sum(
                    1
                    for section in sections
                    if section["key"].startswith("recent_exchange:")
                ),
                "history_omitted": omitted,
                "sections": [
                    {
                        "key": section["key"],
                        "title": section["title"],
                        "chars": section["chars"],
                        "tokens_est": section["tokens_est"],
                        "item_count": section["item_count"],
                        "authority": section["authority"],
                        "rank": section["rank"],
                        "protected": section["protected"],
                    }
                    for section in sections
                ],
            },
        }
