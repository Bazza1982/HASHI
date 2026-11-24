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
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar
from collections.abc import Mapping

from orchestrator.path_presentation import path_presentation_policy
from orchestrator.pcm import PCMDocument, PCMValidationError, load_pcm_document
from orchestrator.hcc import HCC_USAGE_PROMPT, is_hcc_enabled, project_hcc_for_request
from orchestrator import pcm_forget_memory
from orchestrator import pcm_profile as pcm_profile_module
from orchestrator.pcm_profile import (
    CJK_RUN_RE as _CJK_RUN_RE,
    LATIN_TOKEN_RE as _LATIN_TOKEN_RE,
    cjk_query_substrings as _cjk_query_substrings,
    tokenize_multilingual as _tokenize_multilingual,
)
from orchestrator.config_json import (
    ConfigConflictError,
    new_config_json,
    read_config_json,
    write_config_json,
)
from orchestrator.process_resources import path_lock as process_path_lock
from tools.token_tracker import estimate_tokens as _estimate_tokens

CURRENT_REQUEST_SEPARATOR = "\n\n--- CURRENT USER REQUEST — AUTHORITATIVE ---\n"

sys_prompt_logger = logging.getLogger("BridgeU.SysPrompt")
memory_logger = logging.getLogger("BridgeU.Memory")


def _retry_transient_file_access(operation):
    """Retry short-lived Windows sharing violations without hiding real I/O errors."""

    delays = (0.005, 0.01, 0.02, 0.04, 0.08, 0.16, 0.25)
    for attempt in range(len(delays) + 1):
        try:
            return operation()
        except PermissionError:
            if os.name != "nt" or attempt == len(delays):
                raise
            time.sleep(delays[attempt])


def _sys_prompt_path_lock(path: Path) -> threading.RLock:
    return process_path_lock(path)


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


_BGE_CACHE_LOCK = threading.Lock()
_BGE_ENCODER_CACHE: dict[tuple[str, str], "BgeM3Encoder"] = {}

# Conversational framing alone is not evidence that a memory is about a topic.
# Keep the topic words (including one-character Chinese words such as 茶) for
# candidate lookup and relevance; dense embeddings still use the full query.
_MEMORY_QUERY_CJK_FRAME = re.compile(
    r"请问|用户|(?:我|您|你|他|她)?(?:最)?(?:喜欢|偏爱|偏好)(?:喝|吃|看|听|玩)?"
    r"|我的|您的|你的|他的|她的|叫什么名字|叫什么|什么|哪种|哪个|哪些"
    r"|如何|怎么|是否|名字|吗|呢"
)
_MEMORY_QUERY_LATIN_FRAME = frozenset({
    "a", "an", "the", "i", "me", "my", "you", "your", "user",
    "what", "which", "who", "when", "where", "how", "is", "are",
    "do", "does", "did", "please", "like", "likes", "favorite", "favourite",
})


def _memory_topic_query(query: str) -> str:
    text = _MEMORY_QUERY_CJK_FRAME.sub(" ", (query or "").lower())
    text = _LATIN_TOKEN_RE.sub(
        lambda match: " " if match.group() in _MEMORY_QUERY_LATIN_FRAME else match.group(),
        text,
    )
    if _tokenize_multilingual(text):
        return text
    # A broad request such as "我喜欢什么" still asks for preferences.
    return next((word for word in ("喜欢", "偏爱", "偏好") if word in query), "")


class LocalEmbeddingEncoder:
    """Dependency-free multilingual hashed embedding encoder for durable local retrieval."""

    def __init__(self, dim: int = 256):
        self.dim = dim
        self.vector_dim = dim
        self.encoder_id = f"local-hash-v2:blake2b8:cjk-ngram:{dim}"
        self.ready = True
        self.error = None

    def encode(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = _tokenize_multilingual(text)
        if not tokens:
            return vec
        for tok in tokens:
            digest = hashlib.blake2b(
                tok.encode("utf-8"),
                digest_size=8,
                person=b"HASHI-MEM-v2",
            ).digest()
            idx = int.from_bytes(digest, "big") % self.dim
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
    """BGE-M3 ONNX encoder with safe fallback to the multilingual hash encoder."""

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
            or os.environ.get("HASHI_BGE_M3_MODEL_DIR")
            or self.DEFAULT_MODEL_DIR
        )
        self._tokenizer_dir = Path(
            tokenizer_dir
            or os.environ.get("HASHI_BGE_M3_TOKENIZER_DIR")
            or self._model_dir
        )
        self._init()

    @classmethod
    def resolve_model_path(cls, model_dir: str | Path | None = None) -> Path:
        base = Path(
            model_dir
            or os.environ.get("HASHI_BGE_M3_MODEL_DIR")
            or cls.DEFAULT_MODEL_DIR
        )
        onnx_nested = base / "onnx" / "model.onnx"
        if onnx_nested.exists():
            return onnx_nested
        return base / "model.onnx"

    @classmethod
    def model_available(cls, model_dir: str | Path | None = None) -> bool:
        return cls.resolve_model_path(model_dir).exists()

    @classmethod
    def cached(
        cls,
        model_dir: str | Path | None = None,
        tokenizer_dir: str | Path | None = None,
    ) -> "BgeM3Encoder":
        resolved_model = str(
            Path(
                model_dir
                or os.environ.get("HASHI_BGE_M3_MODEL_DIR")
                or cls.DEFAULT_MODEL_DIR
            )
        )
        resolved_tok = str(
            Path(
                tokenizer_dir
                or os.environ.get("HASHI_BGE_M3_TOKENIZER_DIR")
                or resolved_model
            )
        )
        key = (resolved_model, resolved_tok)
        with _BGE_CACHE_LOCK:
            cached = _BGE_ENCODER_CACHE.get(key)
            if cached is not None and cached.ready:
                return cached
            instance = cls(model_dir=resolved_model, tokenizer_dir=resolved_tok)
            if instance.ready:
                _BGE_ENCODER_CACHE[key] = instance
            return instance

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
        return self.DIM if self._ready else self._fallback.vector_dim

    @property
    def encoder_id(self) -> str:
        if self._ready:
            return "bge-m3-onnx-v1:mean-pool-l2:1024"
        return self._fallback.encoder_id

    def _init(self):
        model_path = self.resolve_model_path(self._model_dir)
        if not model_path.exists():
            self._error = f"missing model: {model_path}"
            return

        try:
            import numpy as np
            import onnxruntime as ort
            from transformers import AutoTokenizer
        except Exception as exc:
            self._error = f"dependencies unavailable: {exc}"
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
    def __init__(self, workspace_dir: Path, *, bge_enabled: bool | None = None):
        self.workspace_dir = workspace_dir
        self.db_path = workspace_dir / "bridge_memory.sqlite"
        self.legacy_encoder = LocalEmbeddingEncoder()
        self._bge_requested = False
        self._encoder_degraded_reason: str | None = None
        self.encoder = self._select_encoder(bge_enabled=bge_enabled)
        self._sqlite_vec_supported: bool | None = None
        self._vec_enabled = False
        self._vec_dim: int | None = None
        self._vec_encoder_id: str | None = None
        self._vec_reason: str | None = None
        self._vector_degraded_reason: str | None = None
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

    def _select_encoder(self, *, bge_enabled: bool | None = None):
        from orchestrator.memory_search_mode import get_memory_sync_preference

        pref = get_memory_sync_preference(self.workspace_dir) if bge_enabled is None else bge_enabled
        self._bge_requested = bool(pref is True or (pref is None and BgeM3Encoder.model_available()))
        self._encoder_degraded_reason = None
        if not self._bge_requested:
            return LocalEmbeddingEncoder()
        encoder = BgeM3Encoder.cached()
        if not encoder.ready:
            self._encoder_degraded_reason = encoder.error or "BGE-M3 unavailable; degraded to multilingual hash fallback"
        return encoder

    def _encode(self, text: str) -> tuple[list[float], str, Any]:
        """Return a vector with the identity of the encoder that produced it."""
        encoder = self.encoder
        bge_requested = self._bge_requested
        try:
            return encoder.encode(text), str(encoder.encoder_id), encoder
        except Exception as exc:
            if not bge_requested or encoder is self.legacy_encoder:
                raise
            reason = f"BGE-M3 inference failed ({type(exc).__name__})"
            memory_logger.warning("%s; using local memory retrieval", reason)
            if self.encoder is encoder:
                self._encoder_degraded_reason = reason
                self.encoder = self.legacy_encoder
                # Existing BGE vectors retain their original identity and must
                # not be queried or written as local hashes.
                self._vec_enabled = False
                self._vec_dim = None
                self._vec_reason = reason
            fallback = self.legacy_encoder
            return fallback.encode(text), str(fallback.encoder_id), fallback

    def sync_embeddings(self, *, enable_bge: bool | None = None) -> dict[str, Any]:
        """Apply BGE/local encoder preference, migrate vector tables, and backfill."""
        if enable_bge is not None:
            from orchestrator.memory_search_mode import set_memory_sync_preference

            set_memory_sync_preference(self.workspace_dir, bool(enable_bge))
        self.encoder = self._select_encoder(bge_enabled=enable_bge)
        self._vec_enabled = False
        self._vec_dim = None
        self._vec_encoder_id = None
        self._vec_reason = None
        self._vector_degraded_reason = None
        self._init_db()
        return self.get_vector_status()

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

    def _upsert_vec(
        self, conn: sqlite3.Connection, table: str, key_col: str,
        row_id: int, embedding: list[float], *, encoder_id: str,
    ):
        if not self._vec_enabled or not embedding:
            return
        if self._vec_dim != len(embedding) or encoder_id != self._vec_encoder_id:
            self._disable_vector_index("encoder changed during indexing")
            return
        try:
            conn.execute(
                f"INSERT OR REPLACE INTO {table}({key_col}, embedding) VALUES (?, ?)",
                (row_id, self._vector_blob(embedding)),
            )
        except Exception as exc:
            self._disable_vector_index(f"vector index write failed ({type(exc).__name__})")

    def _disable_vector_index(self, reason: str) -> None:
        self._vector_degraded_reason = reason
        self._vec_reason = reason
        self._vec_enabled = False
        memory_logger.warning("%s; using local text retrieval", reason)

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            pcm_forget_memory.install_schema(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    role TEXT NOT NULL,
                    source TEXT NOT NULL,
                    text TEXT NOT NULL,
                    embedding TEXT NOT NULL,
                    embedding_version TEXT NOT NULL DEFAULT 'legacy-python-hash-v0'
                )
                """
            )
            turn_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(turns)").fetchall()
            }
            if "embedding_version" not in turn_columns:
                conn.execute(
                    "ALTER TABLE turns ADD COLUMN embedding_version "
                    "TEXT NOT NULL DEFAULT 'legacy-python-hash-v0'"
                )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS turns_wip_recovery_source_unique
                ON turns(source) WHERE role = 'recovery'
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
                    embedding TEXT NOT NULL,
                    embedding_version TEXT NOT NULL DEFAULT 'legacy-python-hash-v0',
                    origin TEXT NOT NULL DEFAULT 'legacy',
                    origin_ref TEXT NOT NULL DEFAULT '',
                    session_id TEXT,
                    run_id TEXT,
                    message_id TEXT,
                    promoted_at TEXT
                )
                """
            )
            memory_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(memories)").fetchall()
            }
            for name, definition in (
                ("embedding_version", "TEXT NOT NULL DEFAULT 'legacy-python-hash-v0'"),
                ("origin", "TEXT NOT NULL DEFAULT 'legacy'"),
                ("origin_ref", "TEXT NOT NULL DEFAULT ''"),
                ("session_id", "TEXT"),
                ("run_id", "TEXT"),
                ("message_id", "TEXT"),
                ("promoted_at", "TEXT"),
            ):
                if name not in memory_columns:
                    conn.execute(f"ALTER TABLE memories ADD COLUMN {name} {definition}")
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS memories_origin_ref_unique
                ON memories(origin_ref) WHERE origin_ref != ''
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
            force_vec_backfill = False
            if desired_vec_dim and self._sqlite_vec_supported:
                existing_dims = {
                    dim
                    for dim in (
                        self._vec_table_dim(conn, "memory_vec"),
                        self._vec_table_dim(conn, "turns_vec"),
                    )
                    if dim is not None
                }
                if existing_dims and existing_dims != {desired_vec_dim}:
                    for vec_name in ("memory_vec", "turns_vec"):
                        conn.execute(f"DROP TABLE IF EXISTS {vec_name}")
                    existing_dims = set()
                    force_vec_backfill = True
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
                    self._vec_encoder_id = str(self.encoder.encoder_id)
                elif existing_dims == {desired_vec_dim}:
                    self._vec_enabled = self._vec_table_exists(conn, "memory_vec") and self._vec_table_exists(conn, "turns_vec")
                    self._vec_dim = desired_vec_dim if self._vec_enabled else None
                    self._vec_encoder_id = str(self.encoder.encoder_id) if self._vec_enabled else None
            elif desired_vec_dim:
                self._vec_reason = self._vec_reason or "sqlite-vec unavailable"
            else:
                self._vec_reason = self.encoder.error or "BGE-M3 unavailable; using multilingual hash retrieval"
            self._rebuild_stale_embeddings(conn, force_vec_backfill=force_vec_backfill)
            conn.commit()

    def _rebuild_stale_embeddings(
        self,
        conn: sqlite3.Connection,
        *,
        force_vec_backfill: bool = False,
    ) -> None:
        """Rebuild derived vectors whose persisted encoder identity is stale."""

        encoder_id = str(self.encoder.encoder_id)
        for table, text_column, vec_table, key_column in (
            ("turns", "text", "turns_vec", "turn_id"),
            ("memories", "content", "memory_vec", "memory_id"),
        ):
            if force_vec_backfill:
                rows = conn.execute(
                    f"SELECT id, {text_column} AS body, embedding_version FROM {table}"
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT id, {text_column} AS body, embedding_version FROM {table} "
                    "WHERE embedding_version != ? OR embedding_version IS NULL",
                    (encoder_id,),
                ).fetchall()
            for row in rows:
                embedding, row_encoder_id, _ = self._encode(str(row["body"] or ""))
                if row["embedding_version"] != row_encoder_id:
                    conn.execute(
                        f"UPDATE {table} SET embedding = ?, embedding_version = ? WHERE id = ?",
                        (json.dumps(embedding), row_encoder_id, int(row["id"])),
                    )
                self._upsert_vec(
                    conn,
                    vec_table,
                    key_column,
                    int(row["id"]),
                    embedding,
                    encoder_id=row_encoder_id,
                )

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

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
        parts: list[str] = []
        seen: set[str] = set()
        for tok in _LATIN_TOKEN_RE.findall(q):
            if len(tok) > 1 and tok.upper() not in reserved and tok not in seen:
                seen.add(tok)
                parts.append(tok)
        for term in _cjk_query_substrings(q):
            if term not in seen:
                seen.add(term)
                parts.append(term)
        if not parts:
            return ""
        return " OR ".join(f'"{p}"' for p in parts[:16])

    def record_turn(self, role: str, source: str, text: str) -> int | None:
        clean = (text or "").strip()
        if not clean:
            return None
        recorded_at = self._now()
        embedding, encoder_id, _ = self._encode(clean)
        emb = json.dumps(embedding)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if pcm_forget_memory.blocked(conn, timestamps=(recorded_at,)):
                return None
            cur = conn.execute(
                "INSERT INTO turns "
                "(ts, role, source, text, embedding, embedding_version) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (recorded_at, role, source, clean, emb, encoder_id),
            )
            if cur.lastrowid is not None:
                self._upsert_vec(conn, "turns_vec", "turn_id", int(cur.lastrowid), embedding, encoder_id=encoder_id)
            conn.commit()
            return int(cur.lastrowid) if cur.lastrowid is not None else None

    def record_recovery_capsule(self, content: str, *, origin_ref: str) -> int | None:
        """Idempotently add one deterministic WIP capsule to quoted history."""

        clean = (content or "").strip()
        reference = str(origin_ref or "").strip()
        if not clean or not reference:
            return None
        source = f"wip-recovery:{reference}"
        embedding, encoder_id, _ = self._encode(clean)
        encoded_embedding = json.dumps(embedding)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if pcm_forget_memory.blocked(conn, origin_ref=source):
                return None
            conn.execute(
                """
                INSERT OR IGNORE INTO turns(
                    ts, role, source, text, embedding, embedding_version
                ) VALUES (?, 'recovery', ?, ?, ?, ?)
                """,
                (
                    self._now(),
                    source,
                    clean,
                    encoded_embedding,
                    encoder_id,
                ),
            )
            row = conn.execute(
                "SELECT id, text FROM turns WHERE role = 'recovery' AND source = ?",
                (source,),
            ).fetchone()
            if row is not None and str(row["text"] or "") != clean:
                raise RuntimeError(
                    "recovery capsule source digest resolved to different content"
                )
            if row is not None:
                conn.execute(
                    "UPDATE turns SET embedding = ?, embedding_version = ? WHERE id = ?",
                    (encoded_embedding, encoder_id, int(row["id"])),
                )
                self._upsert_vec(
                    conn,
                    "turns_vec",
                    "turn_id",
                    int(row["id"]),
                    embedding,
                    encoder_id=encoder_id,
                )
            conn.commit()
            return int(row["id"]) if row is not None else None

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
            conn.execute("BEGIN IMMEDIATE")
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
            if pcm_forget_memory.blocked(
                conn, timestamps=(user_ts, assistant_ts), origin_ref=origin_ref
            ):
                return None
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

    def record_memory(
        self,
        memory_type: str,
        source: str,
        content: str,
        importance: float = 1.0,
        *,
        origin: str = "legacy",
        origin_ref: str = "",
        session_id: str | None = None,
        run_id: str | None = None,
        message_id: str | None = None,
        promoted_at: str | None = None,
    ) -> int | None:
        clean = (content or "").strip()
        if not clean:
            return None
        recorded_at = self._now()
        embedding, encoder_id, _ = self._encode(clean)
        emb = json.dumps(embedding)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if pcm_forget_memory.blocked(
                conn, timestamps=(recorded_at,), origin_ref=origin_ref
            ):
                return None
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO memories (
                    ts, memory_type, source, content, importance, embedding, embedding_version,
                    origin, origin_ref, session_id, run_id, message_id, promoted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recorded_at,
                    memory_type,
                    source,
                    clean,
                    float(importance),
                    emb,
                    encoder_id,
                    str(origin or "legacy"),
                    str(origin_ref or ""),
                    str(session_id) if session_id else None,
                    str(run_id) if run_id else None,
                    str(message_id) if message_id else None,
                    str(promoted_at) if promoted_at else None,
                ),
            )
            inserted = cur.rowcount == 1
            memory_id = int(cur.lastrowid) if inserted and cur.lastrowid is not None else None
            if memory_id is None and origin_ref:
                existing = conn.execute(
                    "SELECT id FROM memories WHERE origin_ref = ?", (str(origin_ref),)
                ).fetchone()
                memory_id = int(existing["id"]) if existing is not None else None
            if inserted and memory_id is not None:
                conn.execute(
                    "INSERT INTO memory_fts (content, memory_id, source) VALUES (?, ?, ?)",
                    (clean, memory_id, source),
                )
                self._upsert_vec(conn, "memory_vec", "memory_id", memory_id, embedding, encoder_id=encoder_id)
            conn.commit()
        return memory_id

    def memory_origin_exists(self, origin_ref: str) -> bool:
        clean = str(origin_ref or "").strip()
        if not clean:
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM memories WHERE origin_ref = ?", (clean,)
            ).fetchone()
        return row is not None

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
        """Retrieve relevant memories without backfilling unrelated recency."""

        query_text = (query or "").strip()
        if not query_text:
            return []

        topic_query = _memory_topic_query(query_text)
        safe_query = self._safe_query(topic_query)
        cjk_terms = _cjk_query_substrings(topic_query)
        q_tokens = set(_tokenize_multilingual(topic_query))
        q_vec, query_encoder_id, query_encoder = self._encode(query_text)
        candidates: dict[int, dict[str, Any]] = {}
        if self._vec_enabled and (
            query_encoder_id != self._vec_encoder_id or len(q_vec) != self._vec_dim
        ):
            self._disable_vector_index("encoder changed during retrieval")

        with self._connect() as conn:
            if self._vec_enabled:
                try:
                    rows = conn.execute(
                        """
                        SELECT m.id, m.ts, m.memory_type, m.source, m.content, m.importance,
                               m.embedding, m.embedding_version, v.distance
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
                except Exception as exc:
                    self._disable_vector_index(
                        f"vector index read failed ({type(exc).__name__})"
                    )
            if safe_query:
                try:
                    rows = conn.execute(
                        """
                        SELECT m.id, m.ts, m.memory_type, m.source, m.content, m.importance,
                               m.embedding, m.embedding_version
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

            if cjk_terms:
                escaped_clauses = []
                params: list[str] = []
                for term in cjk_terms:
                    escaped = (
                        term.replace("\\", "\\\\")
                        .replace("%", "\\%")
                        .replace("_", "\\_")
                    )
                    escaped_clauses.append("content LIKE ? ESCAPE '\\'")
                    params.append(f"%{escaped}%")
                rows = conn.execute(
                    f"""
                    SELECT id, ts, memory_type, source, content, importance,
                           embedding, embedding_version
                    FROM memories
                    WHERE {' OR '.join(escaped_clauses)}
                    ORDER BY id DESC
                    LIMIT 40
                    """,
                    params,
                ).fetchall()
                for row in rows:
                    existing = candidates.get(row["id"], {})
                    existing.update(dict(row))
                    existing["_text_match"] = True
                    candidates[row["id"]] = existing

            if not self._vec_enabled:
                recent_rows = conn.execute(
                    """
                    SELECT id, ts, memory_type, source, content, importance,
                           embedding, embedding_version
                    FROM memories
                    ORDER BY id DESC
                    LIMIT 200
                    """
                ).fetchall()
                for row in recent_rows:
                    candidates.setdefault(row["id"], dict(row))

        is_dense_neural = (
            bool(getattr(query_encoder, "ready", False))
            and not isinstance(query_encoder, LocalEmbeddingEncoder)
            and not (isinstance(query_encoder, BgeM3Encoder) and not query_encoder.ready)
        )
        scoring_now = now or datetime.now(timezone.utc)
        if scoring_now.tzinfo is None:
            scoring_now = scoring_now.replace(tzinfo=timezone.utc)
        now_epoch = scoring_now.timestamp()
        half_life_seconds = self.recency_half_life_days * 86400.0
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in candidates.values():
            sim = 0.0
            encoder_matches = row.get("embedding_version") == query_encoder_id
            if encoder_matches and row.get("distance") is not None:
                try:
                    sim = max(0.0, 1.0 - float(row["distance"]))
                except Exception:
                    sim = 0.0
            elif encoder_matches and row.get("embedding"):
                try:
                    emb = json.loads(row["embedding"])
                except Exception:
                    emb = []
                if len(emb) == len(q_vec):
                    sim = query_encoder.cosine(q_vec, emb)
            vector_score = max(0.0, min(1.0, float(sim)))
            row.pop("_text_match", None)
            row.pop("_vector_match", None)
            content = str(row.get("content") or "").lower()
            doc_tokens = set(_tokenize_multilingual(content))
            # CJK single-character topic words may be embedded in a longer
            # run; the hash tokenizer alone does not include those unigrams.
            matched = {
                token for token in q_tokens
                if token in doc_tokens
                or (_CJK_RUN_RE.fullmatch(token) and token in content)
            }
            text_score = len(matched) / len(q_tokens) if q_tokens else 0.0

            relevant = text_score > 0.0 or (
                is_dense_neural and vector_score >= 0.35
            )
            if not relevant:
                continue

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

    def forget_window(
        self,
        *,
        start: datetime,
        end: datetime,
        naive_timezone: str | None = None,
        origin_refs: tuple[str, ...] = (),
        quarantine_late: bool = True,
    ) -> dict[str, int]:
        """Remove selected derived PCM rows while retaining audit transcripts."""
        pcm_forget_memory.validate_window(start, end)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            result = pcm_forget_memory.forget_window(
                conn, start=start, end=end,
                naive_timezone=naive_timezone, origin_refs=origin_refs,
                quarantine_late=quarantine_late,
            )
            conn.commit()
            return result

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
            "encoder_id": str(self.encoder.encoder_id),
            "encoder_dim": self.encoder.vector_dim,
            "encoder_error": self._encoder_degraded_reason or self.encoder.error,
            "bge_requested": bool(self._bge_requested),
            "degraded": bool(self._encoder_degraded_reason or self._vector_degraded_reason),
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

        if self._encoder_degraded_reason or self._vector_degraded_reason:
            status["overall_status"] = "degraded"
        elif status["vec_enabled"]:
            fully_backfilled = (
                status["coverage"]["memories"] >= 0.999
                and status["coverage"]["turns"] >= 0.999
            )
            status["overall_status"] = "fully_upgraded" if fully_backfilled else "partially_upgraded"
        elif status["encoder_ready"] and status["sqlite_vec_supported"]:
            status["overall_status"] = "upgrade_available_not_enabled"

        return status


def sys_prompt_deletion_revision(
    slot: str, state: dict[str, object], *, agent_name: str = "",
) -> str:
    """Bind confirmation to one Agent, slot, full text and active state."""
    payload = json.dumps(
        {"agent": agent_name, "slot": str(slot),
         "text": str(state.get("text") or ""), "active": bool(state.get("active"))},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


class SysPromptSemanticConflict(RuntimeError):
    """The target slot changed after the caller observed or confirmed it."""


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
            self._data: dict = self._normalize(self._read_document())

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
            if raw is None:
                continue
            if not isinstance(raw, dict):
                raise TypeError(f"system prompt slot {slot} must be a JSON object")
            text = raw.get("text", "")
            active = raw.get("active", False)
            if not isinstance(text, str) or type(active) is not bool:
                raise TypeError(
                    f"system prompt slot {slot} text/active has an invalid type"
                )
            normalized[slot] = {
                "text": text,
                "active": active,
            }
        return normalized

    def _read_document(self):
        try:
            return _retry_transient_file_access(
                lambda: read_config_json(self.state_path)
            )
        except FileNotFoundError:
            return new_config_json(self.state_path)

    def _refresh_locked(self) -> None:
        self._data = self._normalize(self._read_document())

    @classmethod
    def _validate_slot_name(cls, n: str) -> None:
        if n not in cls.SLOTS:
            raise ValueError("system prompt slot must be 1..10")

    def _mutate_locked(
        self,
        n: str,
        command: str,
        *,
        text: str | None = None,
        expected_slot: dict[str, object] | None = None,
        max_conflicts: int = 4,
    ) -> dict[str, object]:
        self._validate_slot_name(n)
        if command in {"save", "replace"} and not isinstance(text, str):
            raise TypeError("save/replace requires text")
        expected = dict(expected_slot) if expected_slot is not None else None
        if expected is not None:
            expected = self._normalize({n: expected})[n]

        for attempt in range(max_conflicts + 1):
            source = self._read_document()
            current = self._normalize(source)[n]
            if expected is None:
                expected = dict(current)
            if current != expected:
                raise SysPromptSemanticConflict(
                    f"system prompt slot {n} changed after it was observed"
                )

            updated = dict(current)
            if command == "save":
                if current["text"]:
                    raise SysPromptSemanticConflict(
                        f"system prompt slot {n} is occupied; use confirmed replace"
                    )
                updated = {"text": text, "active": False}
            elif command == "replace":
                updated["text"] = text
            elif command == "activate":
                if not current["text"]:
                    raise ValueError("cannot activate an empty system prompt slot")
                updated["active"] = True
            elif command == "deactivate":
                updated["active"] = False
            elif command == "delete":
                updated = {"text": "", "active": False}
            else:
                raise ValueError(f"unsupported system prompt mutation: {command}")

            candidate = source.copy()
            candidate[n] = updated
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                write_config_json(self.state_path, candidate)
            except ConfigConflictError:
                if attempt == max_conflicts:
                    raise
                continue
            self._data = self._normalize(candidate)
            return updated
        raise AssertionError("unreachable")

    def _slot(self, n: str) -> dict:
        return self.get_slot(n)

    def get_slot(self, n: str) -> dict[str, object]:
        self._validate_slot_name(n)
        with self._lock:
            self._refresh_locked()
            return dict(self._data[n])

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

    def activate(
        self, n: str, *, expected_slot: dict[str, object] | None = None
    ) -> str:
        with self._lock:
            self._mutate_locked(n, "activate", expected_slot=expected_slot)
        prefix = "Global slot" if self.scope == "global" else "Slot"
        return f"{prefix} {n} activated."

    def deactivate(
        self, n: str, *, expected_slot: dict[str, object] | None = None
    ) -> str:
        with self._lock:
            self._mutate_locked(n, "deactivate", expected_slot=expected_slot)
        prefix = "Global slot" if self.scope == "global" else "Slot"
        return f"{prefix} {n} deactivated."

    def save(
        self,
        n: str,
        text: str,
        *,
        expected_slot: dict[str, object] | None = None,
    ) -> str:
        with self._lock:
            self._mutate_locked(n, "save", text=text, expected_slot=expected_slot)
        if self.scope == "global":
            return f"Global slot {n} saved (inactive). Use /sys global {n} on to activate."
        return f"Slot {n} saved (inactive). Use /sys {n} on to activate."

    def replace(
        self,
        n: str,
        text: str,
        *,
        expected_slot: dict[str, object] | None = None,
    ) -> str:
        with self._lock:
            self._mutate_locked(n, "replace", text=text, expected_slot=expected_slot)
        prefix = "Global slot" if self.scope == "global" else "Slot"
        return f"{prefix} {n} updated."

    def delete(
        self, n: str, *, expected_slot: dict[str, object] | None = None
    ) -> str:
        with self._lock:
            self._mutate_locked(n, "delete", expected_slot=expected_slot)
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

    DEFAULT_PROMPT_TOKEN_BUDGET: ClassVar[int] = 64_000
    PROMPT_TOKEN_BUDGETS: ClassVar[dict[str, int]] = {
        "codex-cli": DEFAULT_PROMPT_TOKEN_BUDGET,
        "claude-cli": DEFAULT_PROMPT_TOKEN_BUDGET,
        "grok-cli": DEFAULT_PROMPT_TOKEN_BUDGET,
        "dsh-cli": DEFAULT_PROMPT_TOKEN_BUDGET,
        "openrouter-api": DEFAULT_PROMPT_TOKEN_BUDGET,
        "deepseek-api": DEFAULT_PROMPT_TOKEN_BUDGET,
        "xai-api": DEFAULT_PROMPT_TOKEN_BUDGET,
        "hashi-api": DEFAULT_PROMPT_TOKEN_BUDGET,
        # Local inference never uses the generic 64K budget as an operating
        # budget; its own capacity resolver remains authoritative.
        "ollama-api": pcm_profile_module.LOCAL_PROMPT_TOKEN_BUDGET,
        "hli-api": pcm_profile_module.LOCAL_PROMPT_TOKEN_BUDGET,
    }
    # Compatibility alias for extensions that previously adjusted this class
    # variable. Values are token counts now; the old character-cap semantics
    # were the continuity bug this policy replaces.
    PROMPT_BUDGETS: ClassVar[dict[str, int]] = PROMPT_TOKEN_BUDGETS
    PROMPT_TOKEN_BUDGET_PROVENANCE: ClassVar[str] = (
        "hashi_pcm_non_her_64k_tokens_v1"
    )
    HISTORY_CAPSULE_EXCERPT_CHARS: ClassVar[int] = 320
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
    _TRANSPORT_ORDER_STRIDE: ClassVar[int] = 100_000_000
    _TRANSPORT_ORDER_SLOTS: ClassVar[dict[str, int]] = {
        "permanent_system": 1,
        "instance_global_sys": 1,
        "instance_path_presentation": 2,
        "agent_local_sys": 1,
        "hcc_usage": 2,
        "hcc": 3,
        "current_user_request": 1,
        "permanent_memory": 1,
        "relevant_long_term_memory": 2,
        "time": 1,
        "active_runtime_instructions": 2,
        "skills_catalogue": 4,
        "tools_catalogue": 5,
        "persona": 1,
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
        self.saved_memory_injection_enabled = True

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

    @classmethod
    def _stable_transport_order(cls, section: dict[str, Any]) -> int:
        """Return a section-identity order that survives optional omissions.

        Fixed backends compare every typed field when calculating a PCM delta.
        A list index is therefore not a safe presentation order: omitting an
        initial-only history or memory section would otherwise make every
        following unchanged section look updated.  Known sections occupy
        semantic slots, exchanges retain chronological sequence, and extension
        sections receive a deterministic identity-derived position between
        active runtime instructions and the catalogues.
        """

        key = str(section.get("key") or "")
        stride = cls._TRANSPORT_ORDER_STRIDE
        metadata = section.get("metadata") or {}
        conversation_at = str(metadata.get("conversation_at") or "").strip()
        if section.get("authority") == "history" and conversation_at:
            try:
                parsed = datetime.fromisoformat(
                    conversation_at.replace("Z", "+00:00")
                )
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return stride + max(
                    0,
                    int(parsed.astimezone(timezone.utc).timestamp() * 1_000_000),
                )
            except (TypeError, ValueError, OSError, OverflowError):
                pass
        if key.startswith("recent_exchange:"):
            sequence = int(metadata.get("sequence") or 0)
            return stride + max(0, min(sequence, stride - 1))
        if key.startswith("extra:"):
            identity = int.from_bytes(
                hashlib.sha256(key.encode("utf-8")).digest()[:8], "big"
            )
            return (3 * stride) + (identity % stride)
        slot = cls._TRANSPORT_ORDER_SLOTS.get(key)
        if slot is not None:
            return slot * stride
        identity = int.from_bytes(
            hashlib.sha256(key.encode("utf-8")).digest()[:8], "big"
        )
        return (3 * stride) + (identity % stride)

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

    def _build_time_fyi(self, *, minimal: bool = False) -> str:
        """Build accurate local date/time information for one external turn."""

        now = datetime.now().astimezone()
        zone_name = now.tzname() or "local"
        offset = now.strftime("%z")
        offset = f"{offset[:3]}:{offset[3:]}" if len(offset) == 5 else offset
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        base = f"Current local time: {now_str} {zone_name} (UTC{offset})."
        if minimal:
            return base
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
        extra_sections: list[tuple[str, str] | tuple[str, str, dict[str, Any]]] | None = None,
        context_profile: str | None = None,
        recent_exchanges: list[dict[str, Any]] | None = None,
        explicit_history_context: bool = False,
        prompt_budget_tokens: int | None = None,
        pcm_profile: str = pcm_profile_module.PCM_PROFILE_CLOUD,
    ) -> str:
        return self.build_prompt_payload(
            user_prompt,
            engine,
            incremental=incremental,
            extra_sections=extra_sections,
            context_profile=context_profile,
            recent_exchanges=recent_exchanges,
            explicit_history_context=explicit_history_context,
            prompt_budget_tokens=prompt_budget_tokens,
            pcm_profile=pcm_profile,
        )["final_prompt"]

    @classmethod
    def _history_excerpt(
        cls, value: Any, *, max_chars: int | None = None,
    ) -> str:
        """Return a bounded semantic cue without treating it as a full summary.

        Handoff and cross-session messages can contain an entire typed envelope.
        In those cases the current-request block is much more useful than the
        repeated boilerplate at the start of the value.  The result remains an
        explicitly labelled excerpt, never a claim that HASHI reconstructed the
        full omitted exchange.
        """

        text = str(value or "").replace("\r\n", "\n").strip()
        marker = CURRENT_REQUEST_SEPARATOR.strip()
        if marker in text:
            text = text.split(marker, 1)[1]
            next_section = re.search(r"\n--- [A-Z][^\n]* ---\n", text)
            if next_section:
                text = text[: next_section.start()]
        elif "\nOriginal task:\n" in text:
            text = text.split("\nOriginal task:\n", 1)[1]
            if "\nAssistant result:\n" in text:
                text = text.split("\nAssistant result:\n", 1)[0]
        text = re.sub(r"\s+", " ", text).strip()
        limit = cls.HISTORY_CAPSULE_EXCERPT_CHARS
        if max_chars is not None:
            limit = min(limit, max_chars)
        if len(text) <= limit:
            return text
        head = max(1, (limit * 2) // 3)
        tail = max(1, limit - head - 3)
        return f"{text[:head].rstrip()} … {text[-tail:].lstrip()}"

    @classmethod
    def _history_capsule_row(
        cls,
        exchange: dict[str, Any] | None,
        *,
        sequence: int,
        source_text: str,
        excerpt_chars: int | None = None,
    ) -> str:
        source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        if exchange is None:
            excerpt = cls._history_excerpt(source_text, max_chars=excerpt_chars)
            return (
                f"Exchange sequence={sequence}; user_ts=unknown-time; "
                f"assistant_ts=unknown-time; source_sha256={source_hash}\n"
                f"HISTORY_EXCERPT: {excerpt or '(empty)'}"
            )
        user_excerpt = cls._history_excerpt(
            exchange.get("user_text"), max_chars=excerpt_chars,
        )
        assistant_excerpt = cls._history_excerpt(
            exchange.get("assistant_text"), max_chars=excerpt_chars,
        )
        lines = [
            f"Exchange sequence={sequence}; user_ts={exchange.get('user_ts') or 'unknown-time'}; "
            f"assistant_ts={exchange.get('assistant_ts') or 'unknown-time'}; "
            f"source_sha256={source_hash}"
        ]
        if user_excerpt or exchange.get("user_message_id") is not None:
            lines.append(f"USER_EXCERPT: {user_excerpt or '(empty)'}")
        if assistant_excerpt or exchange.get("assistant_message_id") is not None:
            lines.append(f"ASSISTANT_EXCERPT: {assistant_excerpt or '(empty)'}")
        return "\n".join(lines)

    def _resolve_prompt_token_budget(
        self,
        engine: str,
        override: int | None,
    ) -> tuple[int | None, str]:
        if str(engine or "").strip().lower() == "her-v2":
            return None, "her_v2_managed_compaction"
        if isinstance(override, int) and not isinstance(override, bool) and override > 0:
            return override, "backend_config_pcm_prompt_token_budget"
        normalized = str(engine or "").strip().lower()
        return (
            self.PROMPT_TOKEN_BUDGETS.get(
                normalized,
                self.DEFAULT_PROMPT_TOKEN_BUDGET,
            ),
            self.PROMPT_TOKEN_BUDGET_PROVENANCE,
        )

    @staticmethod
    def _render_section(section: dict[str, Any]) -> str:
        return f"--- {section['title']} ---\n\n{section['text']}"

    def _render_pcm_prompt(self, sections: list[dict[str, Any]]) -> str:
        groups: dict[str, list[dict[str, Any]]] = {}
        for section in sections:
            groups.setdefault(str(section["authority"]), []).append(section)

        parts: list[str] = []
        for authority in ("permanent_system", "global_system", "local_system"):
            parts.extend(self._render_section(item) for item in groups.get(authority, []))

        current = groups.get("current_user", [])
        if current:
            parts.append(
                CURRENT_REQUEST_SEPARATOR.strip()
                + "\nThis request overrides conflicting earlier user requests, "
                "not system instructions.\n\n"
                + current[0]["text"]
            )

        history = groups.get("history", [])
        if history:
            parts.append(
                "--- RECENT COMPLETED EXCHANGES — CONTEXT ONLY ---\n\n"
                + "\n\n".join(item["text"] for item in history)
            )

        for authority in ("memory", "runtime_context"):
            parts.extend(self._render_section(item) for item in groups.get(authority, []))

        persona = groups.get("persona", [])
        if persona:
            parts.append(
                "--- CURRENT PRESENTATION PERSONA ---\n\n"
                + persona[0]["text"]
            )
        return "\n\n".join(parts).strip()

    @classmethod
    def render_incremental_payload(
        cls,
        payload: dict[str, Any],
        previous_digests: Mapping[str, str] | None,
    ) -> dict[str, Any]:
        """Rebuild a minimal final prompt for a persistent Engine Session.

        The static PCM snapshot was sent at session bootstrap, so an
        incremental turn carries only the current request plus the sections
        that actually changed (by content digest) and explicit removal
        notices. Engines that cannot literally retract prior messages receive
        changed sections with an explicit supersession marker; removals are
        announced, never presented as erased history.
        """
        envelope = payload.get("envelope") or {}
        snapshot = payload.get("transport_snapshot") or {}
        sections = [
            dict(item)
            for item in snapshot.get("sections") or []
            if isinstance(item, Mapping)
        ]
        digests = {
            str(item.get("key") or ""): str(item.get("content_sha256") or "")
            for item in envelope.get("sections") or []
            if isinstance(item, Mapping)
        }
        previous = {
            str(key): str(value or "")
            for key, value in (previous_digests or {}).items()
        }
        removed_keys = [
            str(key)
            for key in snapshot.get("removed_section_keys") or []
            if str(key) and str(key) in previous
        ]
        changed: list[dict[str, Any]] = []
        for section in sections:
            key = str(section.get("key") or "")
            if key == "current_user_request":
                continue
            if digests.get(key) != previous.get(key):
                changed.append(section)
        current = next(
            (
                item
                for item in sections
                if item.get("key") == "current_user_request"
            ),
            None,
        )
        if not changed and not removed_keys:
            projected = (
                str(current.get("text") or "")
                if isinstance(current, Mapping)
                else ""
            )
            payload["final_prompt"] = projected
            audit = payload.setdefault("audit", {})
            audit["pcm_incremental"] = {
                "mode": "request_only",
                "changed_sections": 0,
                "removed_sections": 0,
                "sent_chars": len(projected),
                "sent_tokens_est": _estimate_tokens(projected),
            }
            # The incremental projection replaces the full assembled prompt;
            # keep the size audit consistent with what is actually sent.
            audit["final_prompt_chars_after_budget"] = len(projected)
            audit["final_prompt_tokens_after_budget"] = _estimate_tokens(projected)
            return payload
        parts: list[str] = []
        for section in changed:
            parts.append(
                f"--- {section.get('title') or section.get('key')} — UPDATED; "
                "supersedes the earlier version ---\n\n"
                + str(section.get("text") or "")
            )
        for key in removed_keys:
            parts.append(
                f"--- {key} — REMOVED ---\n\n"
                "This section is no longer effective; disregard its earlier content."
            )
        if isinstance(current, Mapping) and str(current.get("text") or ""):
            parts.append(
                CURRENT_REQUEST_SEPARATOR.strip() + "\n\n" + str(current["text"])
            )
        projected = "\n\n".join(parts).strip()
        payload["final_prompt"] = projected
        audit = payload.setdefault("audit", {})
        audit["pcm_incremental"] = {
            "mode": "delta",
            "changed_sections": len(changed),
            "removed_sections": len(removed_keys),
            "sent_chars": len(projected),
            "sent_tokens_est": _estimate_tokens(projected),
        }
        audit["final_prompt_chars_after_budget"] = len(projected)
        audit["final_prompt_tokens_after_budget"] = _estimate_tokens(projected)
        return payload

    def build_prompt_payload(
        self,
        user_prompt: str,
        engine: str,
        incremental: bool = False,
        extra_sections: list[tuple[str, str] | tuple[str, str, dict[str, Any]]] | None = None,
        inject_memory: bool = True,
        context_profile: str | None = None,
        recent_exchanges: list[dict[str, Any]] | None = None,
        explicit_history_context: bool = False,
        prompt_budget_tokens: int | None = None,
        pcm_profile: str = pcm_profile_module.PCM_PROFILE_CLOUD,
    ) -> dict[str, Any]:
        """Build PCM with token-aware, traceable non-HER history fallback."""

        pcm_profile = pcm_profile_module.normalize_pcm_profile(pcm_profile)
        local_profile = (
            str(pcm_profile).strip().casefold() == pcm_profile_module.PCM_PROFILE_LOCAL
        )
        document = self._load_pcm()
        sections: list[dict[str, Any]] = []
        hcc_enabled = bool(
            document and self.system_md and is_hcc_enabled(self.system_md.parent)
        )
        hcc_text = (document.hcc or "") if document else ""
        hcc_included = hcc_enabled and bool(hcc_text.strip())
        removed_section_keys = [] if hcc_included else ["hcc", "hcc_usage"]

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
        elif self.global_sys_prompt_manager is not None:
            removed_section_keys.append("instance_global_sys")
        add_section(
            "instance_path_presentation",
            "INSTANCE PATH PRESENTATION",
            path_presentation_policy(),
            "global_system",
            protected=True,
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
        elif self.sys_prompt_manager is not None:
            removed_section_keys.append("agent_local_sys")

        add_section(
            "current_user_request",
            "CURRENT USER REQUEST",
            user_prompt,
            "current_user",
            protected=True,
        )

        if hcc_included:
            add_section(
                "hcc_usage", "HCC USAGE INSTRUCTIONS", HCC_USAGE_PROMPT,
                "local_system", protected=True,
            )
            hcc_body = hcc_text
            if local_profile:
                # Deterministic relevance projection: keep the cache entries
                # that lexically relate to the current request, bounded and in
                # document order; the full cache stays readable via file_read.
                hcc_body = project_hcc_for_request(
                    hcc_text,
                    user_prompt,
                    pcm_profile_module.LOCAL_HCC_PROJECTION_CHARS,
                )
            add_section(
                "hcc", "HASHI CONTEXT CACHE", hcc_body,
                "runtime_context", protected=True,
                metadata={"source": "agent.md#hcc", "replace_only": True},
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
            and not managed_history
            and (
                bool(explicit_history_context)
                or (inject_memory and self.turns_injection_enabled)
            )
        )
        history_limit = self.MAX_RECENT_EXCHANGES
        exchanges = []
        if should_inject_history:
            if recent_exchanges is not None:
                exchanges = list(recent_exchanges)[-history_limit:]
            else:
                # Compatibility for legacy callers and migration tooling. The
                # live runtime always supplies a Session-scoped timeline.
                getter = self.memory_store.get_completed_exchanges
                kwargs: dict[str, Any] = {"limit": history_limit}
                try:
                    if "after_epoch" in inspect.signature(getter).parameters:
                        from orchestrator.fresh_context import workspace_cutoff_epoch

                        workspace = getattr(self.memory_store, "workspace_dir", None)
                        if workspace is not None:
                            kwargs["after_epoch"] = workspace_cutoff_epoch(workspace)
                except (TypeError, ValueError):
                    pass
                exchanges = getter(**kwargs)
        capsule_rows: dict[str, str] = {}
        capsule_sources: dict[
            str, tuple[dict[str, Any] | None, int, str]
        ] = {}
        for exchange in exchanges:
            sequence = int(exchange.get("sequence") or exchange.get("exchange_id") or 0)
            user_ts = str(exchange.get("user_ts") or "unknown-time")
            assistant_ts = str(exchange.get("assistant_ts") or "unknown-time")
            user_provenance = str(
                exchange.get("user_transcript_provenance") or ""
            ).strip()
            assistant_provenance = str(
                exchange.get("assistant_transcript_provenance") or ""
            ).strip()
            user_label = (
                f"USER [transcript provenance={user_provenance}]"
                if user_provenance
                else "USER"
            )
            assistant_label = (
                f"ASSISTANT [transcript provenance={assistant_provenance}]"
                if assistant_provenance
                else "ASSISTANT"
            )
            exchange_identity = str(exchange.get("exchange_id") or sequence)
            exchange_key = f"recent_exchange:{exchange_identity}"
            exchange_lines = [
                f"Exchange sequence={sequence}; user_ts={user_ts}; assistant_ts={assistant_ts}"
            ]
            if exchange.get("user_message_id") is not None or exchange.get("user_text"):
                exchange_lines.append(f"{user_label}: {exchange.get('user_text', '')}")
            if exchange.get("assistant_message_id") is not None or exchange.get("assistant_text"):
                exchange_lines.append(
                    f"{assistant_label}: {exchange.get('assistant_text', '')}"
                )
            exchange_text = "\n".join(exchange_lines)
            add_section(
                exchange_key,
                "RECENT COMPLETED EXCHANGE",
                exchange_text,
                "history",
                metadata={
                    "sequence": sequence,
                    "exchange_id": exchange.get("exchange_id"),
                    "conversation_at": (
                        exchange.get("user_ts") or exchange.get("assistant_ts")
                    ),
                },
            )
            capsule_exchange = dict(exchange)
            capsule_sources[exchange_key] = (
                capsule_exchange, sequence, exchange_text,
            )
            capsule_rows[exchange_key] = self._history_capsule_row(
                capsule_exchange,
                sequence=sequence,
                source_text=exchange_text,
            )

        if document and document.memory:
            if local_profile:
                # Local keeps the memory units that lexically relate to the
                # current request (bounded, deterministic, no new retrieval
                # subsystem) plus a retrieval reference. The canonical block
                # remains in agent.md and is readable via the authorized file
                # tools; retrieval does not assume memory_search indexes it.
                projected_memory = pcm_profile_module.project_memory_for_request(
                    document.memory,
                    user_prompt,
                    pcm_profile_module.LOCAL_MEMORY_PROJECTION_CHARS,
                )
                add_section(
                    "permanent_memory",
                    "LONG-TERM MEMORY (RELEVANT PROJECTION + REFERENCE)",
                    (projected_memory or "")
                    + "\nFull long-term memory is in agent.md ([memory]); read it "
                    "with file_read when relevant to this request.",
                    "memory",
                    protected=True,
                )
            else:
                add_section(
                    "permanent_memory",
                    "LONG-TERM MEMORY FROM agent.md",
                    document.memory,
                    "memory",
                    protected=True,
                )
        elif document is not None:
            removed_section_keys.append("permanent_memory")

        inject_search = (
            not incremental and inject_memory and self.saved_memory_injection_enabled
        )
        search_limit = (
            pcm_profile_module.LOCAL_MEMORY_SEARCH_LIMIT if local_profile else 6
        )
        retrieved = (
            self.memory_store.retrieve_memories(user_prompt, limit=search_limit)
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

        time_fyi = (
            self._build_time_fyi(minimal=True)
            if local_profile
            and "minimal" in inspect.signature(self._build_time_fyi).parameters
            else self._build_time_fyi()
        )
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

        from orchestrator.frontend_call.context import CallMessageContextSection, CALL_INTERACTION_GUIDANCE

        if not any(isinstance(section, CallMessageContextSection) for section in extra_sections or []):
            # Fixed Sessions treat an omitted section as unchanged. Current-input
            # call policy therefore needs an explicit, narrowly scoped tombstone.
            removed_section_keys.append("call_interaction_policy")

        for raw_section in extra_sections or []:
            if len(raw_section) < 2:
                continue
            title, body = raw_section[0], raw_section[1]
            options = (
                dict(raw_section[2])
                if len(raw_section) > 2 and isinstance(raw_section[2], dict)
                else {}
            )
            if not title or not body:
                continue
            authority = "history" if managed_history_title and title == managed_history_title else "runtime_context"
            section_key = str(
                options.pop("key", f"extra:{str(title).lower().replace(' ', '_')}")
            )
            # A mandatory external transcript remains history even when it
            # must survive budgeting; protection never elevates its authority.
            if options.pop("authority", None) == "history":
                authority = "history"
            protected = bool(options.pop("protected", False))
            add_section(
                section_key,
                str(title),
                str(body),
                authority,
                protected=protected,
                item_count=1,
                metadata=options,
            )
            if (
                authority == "history"
                and section_key.startswith("recent_exchange:")
                and section_key not in capsule_rows
            ):
                source_text = str(body).strip()
                sequence = int(options.get("sequence") or 0)
                capsule_sources[section_key] = (None, sequence, source_text)
                capsule_rows[section_key] = self._history_capsule_row(
                    None, sequence=sequence, source_text=source_text,
                )

            if isinstance(raw_section, CallMessageContextSection):
                # Application-owned presentation policy must reach the model's
                # instruction channel. Camera facts remain the runtime-data
                # section above. JSON metadata cannot mint this typed marker.
                add_section(
                    "call_interaction_policy",
                    "CURRENT CALL INTERACTION",
                    CALL_INTERACTION_GUIDANCE,
                    "local_system",
                    protected=True,
                    item_count=1,
                    metadata={"schema": "hashi.call-interaction-policy", "version": 1},
                )

        if not local_profile:
            # Local relies on native tool schemas + on-demand tool_find and on
            # explicit skill discovery instead of re-sending full catalogues.
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
        else:
            removed_section_keys.extend(("skills_catalogue", "tools_catalogue"))

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
        limit, budget_provenance = self._resolve_prompt_token_budget(
            engine,
            prompt_budget_tokens,
        )
        omitted: list[dict[str, Any]] = []
        capsule_key = "history_continuity_capsule"

        def install_capsule() -> None:
            sections[:] = [section for section in sections if section["key"] != capsule_key]
            if not omitted:
                return
            omitted_keys = [str(item["key"]) for item in omitted]
            body = (
                "COMPACTED HISTORY CONTINUITY CAPSULE\n"
                "HASHI removed the raw bodies of the following oldest complete exchanges "
                "from this request only to meet the non-HER bootstrap token budget. "
                "The exact exchanges remain durable in HASHI's canonical history ledger. "
                "These bounded excerpts are continuity cues, not complete summaries; do not "
                "invent missing detail. Each source_sha256 identifies the exact omitted body.\n\n"
                + "\n\n".join(capsule_rows[key] for key in omitted_keys)
            )
            add_section(
                capsule_key,
                "COMPACTED HISTORY CONTINUITY CAPSULE",
                body,
                "history",
                protected=True,
                item_count=len(omitted_keys),
                metadata={
                    "sequences": [item["sequence"] for item in omitted],
                    "method": "deterministic_salient_excerpt_v1",
                },
            )
            capsule = sections.pop()
            first_history = next(
                (
                    index
                    for index, section in enumerate(sections)
                    if section["authority"] == "history"
                ),
                len(sections),
            )
            sections.insert(first_history, capsule)

        if limit is not None:
            while _estimate_tokens(self._render_pcm_prompt(sections)) > limit:
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
                        "reason": "non_her_prompt_token_budget",
                        "represented_by": capsule_key,
                        "source_content_sha256": hashlib.sha256(
                            removed["text"].encode("utf-8")
                        ).hexdigest(),
                    }
                )
                install_capsule()
            # The excerpts can themselves exceed a small budget after all raw
            # exchanges have been removed. Keep every source receipt and the
            # protected layers; reduce only these derived continuity cues.
            excerpt_chars = self.HISTORY_CAPSULE_EXCERPT_CHARS
            minimum_excerpt_chars = 24
            while (
                omitted
                and _estimate_tokens(self._render_pcm_prompt(sections)) > limit
                and excerpt_chars > minimum_excerpt_chars
            ):
                excerpt_chars = max(minimum_excerpt_chars, excerpt_chars // 2)
                for item in omitted:
                    key = str(item["key"])
                    exchange, sequence, source_text = capsule_sources[key]
                    capsule_rows[key] = self._history_capsule_row(
                        exchange,
                        sequence=sequence,
                        source_text=source_text,
                        excerpt_chars=excerpt_chars,
                    )
                install_capsule()
        final_prompt = self._render_pcm_prompt(sections)
        final_prompt_tokens = _estimate_tokens(final_prompt)
        if hcc_included and limit is not None and final_prompt_tokens > limit:
            raise PCMValidationError(
                "pcm_hcc_capacity_exceeded",
                f"PCM with HCC exceeds the input budget ({final_prompt_tokens} > {limit} "
                "estimated tokens). Reduce the configured cache; it was not truncated.",
            )
        if omitted:
            memory_logger.warning(
                "PCM history compacted: engine=%s omitted_raw=%s retained_raw=%s "
                "capsule=%s limit_tokens=%s before_tokens=%s after_tokens=%s provenance=%s",
                engine,
                len(omitted),
                sum(
                    1
                    for section in sections
                    if section["key"].startswith("recent_exchange:")
                ),
                capsule_key,
                limit,
                _estimate_tokens(final_prompt_unbudgeted),
                final_prompt_tokens,
                budget_provenance,
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
        # Exact section bodies are returned only to the in-process backend
        # transport.  HER v2 fixed sessions use this snapshot to compute a
        # revisioned delta against their last durable acknowledgement.  It is
        # deliberately separate from the audit envelope, which remains
        # content-free.
        transport_sections = [
            {
                "key": section["key"],
                "title": section["title"],
                "text": section["text"],
                "authority": section["authority"],
                "rank": section["rank"],
                "protected": section["protected"],
                "metadata": section["metadata"],
                "order": self._stable_transport_order(section),
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
            "transport_snapshot": {
                "version": 1,
                "sections": transport_sections,
                "removed_section_keys": list(dict.fromkeys(removed_section_keys)),
            },
            "audit": {
                "hcc": {
                    "enabled": hcc_enabled,
                    "included": hcc_included,
                    "chars": len(hcc_text) if hcc_included else 0,
                    "tokens_est": _estimate_tokens(hcc_text) if hcc_included else 0,
                    "content_sha256": (
                        hashlib.sha256(hcc_text.encode("utf-8")).hexdigest()
                        if hcc_included else None
                    ),
                },
                "incremental": incremental,
                "context_profile": context_profile,
                "pcm_profile": str(pcm_profile),
                # Kept as a nullable compatibility field so older audit readers
                # do not mistake a token budget for a character count.
                "budget_limit_chars": None,
                "budget_limit_tokens": limit,
                "budget_unit": "estimated_tokens" if limit is not None else "her_v2_managed",
                "budget_provenance": budget_provenance,
                "budget_applied": bool(omitted),
                "budget_unresolved": limit is not None and final_prompt_tokens > limit,
                "context_chars_before_budget": len(unbudgeted_context_text),
                "context_tokens_before_budget": _estimate_tokens(
                    unbudgeted_context_text
                ),
                "final_prompt_chars_before_budget": len(final_prompt_unbudgeted),
                "final_prompt_chars_after_budget": len(final_prompt),
                "final_prompt_tokens_before_budget": _estimate_tokens(
                    final_prompt_unbudgeted
                ),
                "final_prompt_tokens_after_budget": final_prompt_tokens,
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
                "history_capsule": {
                    "present": bool(omitted),
                    "key": capsule_key if omitted else None,
                    "item_count": len(omitted),
                    "method": (
                        "deterministic_salient_excerpt_v1" if omitted else None
                    ),
                },
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
# HASHI-Fingerprint: hfp_124d2add17274ec0af428f4a79ec93b9
