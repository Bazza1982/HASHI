from __future__ import annotations
import hashlib
import json
import math
import os
import re
import sqlite3
import struct
from datetime import datetime
from pathlib import Path
from typing import Any

from tools.token_tracker import estimate_tokens as _estimate_tokens


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
        self._init_db()

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

    def record_turn(self, role: str, source: str, text: str):
        clean = (text or "").strip()
        if not clean:
            return
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

    def retrieve_memories(self, query: str, limit: int = 6) -> list[dict[str, Any]]:
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
                        candidates[row["id"]] = dict(row)
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
                        candidates[row["id"]] = dict(row)
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
            recency_boost = 0.05 if row["memory_type"] == "episodic" else 0.1
            score = sim + float(row.get("importance", 1.0)) * recency_boost
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
        """Delete all stored conversation turns, keeping memories intact."""
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
    """Manages up to 10 additional system prompt slots per workspace."""

    SLOTS = [str(i) for i in range(1, 11)]

    def __init__(self, workspace_dir: Path):
        self.state_path = workspace_dir / "sys_prompts.json"
        self._data: dict = self._load()

    def _load(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {slot: {"text": "", "active": False} for slot in self.SLOTS}

    def _save(self):
        # Ensure all 10 slots exist
        for slot in self.SLOTS:
            self._data.setdefault(slot, {"text": "", "active": False})
        self.state_path.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")

    def _slot(self, n: str) -> dict:
        self._data.setdefault(n, {"text": "", "active": False})
        return self._data[n]

    def display_all(self) -> str:
        lines = ["*System Prompt Slots:*"]
        for slot in self.SLOTS:
            s = self._slot(slot)
            status = "ON" if s["active"] else "off"
            preview = (s["text"][:60] + "…") if len(s["text"]) > 60 else s["text"]
            lines.append(f"[{slot}] {status} | {preview or '(empty)'}")
        return "\n".join(lines)

    def display_slot(self, n: str) -> str:
        s = self._slot(n)
        status = "ON" if s["active"] else "off"
        text = s["text"] or "(empty)"
        return f"Slot {n} [{status}]:\n{text}"

    def activate(self, n: str) -> str:
        if not self._slot(n)["text"]:
            return f"Slot {n} is empty — save a message first."
        self._data[n]["active"] = True
        self._save()
        return f"Slot {n} activated."

    def deactivate(self, n: str) -> str:
        self._data[n]["active"] = False
        self._save()
        return f"Slot {n} deactivated."

    def save(self, n: str, text: str) -> str:
        self._data[n] = {"text": text, "active": False}
        self._save()
        return f"Slot {n} saved (inactive). Use /sys {n} on to activate."

    def replace(self, n: str, text: str) -> str:
        was_active = self._slot(n).get("active", False)
        self._data[n] = {"text": text, "active": was_active}
        self._save()
        return f"Slot {n} updated."

    def delete(self, n: str) -> str:
        self._data[n] = {"text": "", "active": False}
        self._save()
        return f"Slot {n} cleared."

    def get_active_texts(self) -> list[str]:
        return [
            self._data[slot]["text"]
            for slot in self.SLOTS
            if self._data.get(slot, {}).get("active") and self._data[slot].get("text")
        ]


class BridgeContextAssembler:
    PROMPT_BUDGETS = {
        "codex-cli": 24000,
        "gemini-cli": 24000,
        "claude-cli": 50000,
        "openrouter-api": 35000,
        "ollama-api": 30000,
    }

    def __init__(self, memory_store: BridgeMemoryStore, system_md: Path | None, active_skill_provider=None, sys_prompt_manager=None):
        self.memory_store = memory_store
        self.system_md = system_md
        self.active_skill_provider = active_skill_provider
        self.sys_prompt_manager = sys_prompt_manager
        self.turns_injection_enabled: bool = True
        self.saved_memory_injection_enabled: bool = True

    @property
    def memory_injection_enabled(self) -> bool:
        return self.turns_injection_enabled and self.saved_memory_injection_enabled

    @memory_injection_enabled.setter
    def memory_injection_enabled(self, enabled: bool) -> None:
        value = bool(enabled)
        self.turns_injection_enabled = value
        self.saved_memory_injection_enabled = value

    def _load_system_prompt(self) -> str:
        if not self.system_md:
            return ""
        try:
            if self.system_md.exists():
                return self.system_md.read_text(encoding="utf-8").strip()
        except Exception:
            return ""
        return ""

    def _clip(self, text: str, limit: int, marker: str) -> str:
        t = (text or "").strip()
        if len(t) <= limit:
            return t
        return t[: limit - len(marker) - 2].rstrip() + "\n\n" + marker

    def _apply_budget(self, prompt: str, engine: str) -> str:
        limit = self.PROMPT_BUDGETS.get(engine, 30000)
        if len(prompt) <= limit:
            return prompt

        separator = "\n\n--- NEW REQUEST ---\n"
        if separator not in prompt:
            return prompt[-limit:]

        context_part, request_part = prompt.split(separator, 1)
        request_part = request_part.strip()
        request_budget = min(max(len(request_part), 5000), limit // 2)
        kept_request = request_part[-request_budget:]
        context_budget = max(limit - len(separator) - len(kept_request) - 64, 1200)
        kept_context = self._clip(context_part, context_budget, "[context trimmed for budget]")
        return f"{kept_context}{separator}{kept_request}"

    def _build_time_fyi(self) -> str:
        """Build a soft time-awareness note for the agent."""
        now = datetime.now()
        now_str = now.strftime("%I:%M %p").lstrip("0")
        last_ts = self.memory_store.get_last_user_turn_ts()
        if not last_ts:
            return f"[FYI: You received this message at {now_str}.]"
        try:
            last_dt = datetime.fromisoformat(last_ts)
            delta = now - last_dt
            total_seconds = int(delta.total_seconds())
            if total_seconds < 60:
                gap = f"{total_seconds}s ago"
            elif total_seconds < 3600:
                gap = f"{total_seconds // 60}m ago"
            elif total_seconds < 86400:
                hours = total_seconds // 3600
                mins = (total_seconds % 3600) // 60
                gap = f"{hours}h {mins}m ago" if mins else f"{hours}h ago"
            else:
                days = total_seconds // 86400
                gap = f"{days} day{'s' if days != 1 else ''} ago"
            last_str = last_dt.strftime("%I:%M %p").lstrip("0")
            return f"[FYI: You received this message at {now_str}. Last message from user was at {last_str} — {gap}.]"
        except Exception:
            return f"[FYI: You received this message at {now_str}.]"

    def build_prompt(
        self,
        user_prompt: str,
        engine: str,
        incremental: bool = False,
        extra_sections: list[tuple[str, str]] | None = None,
    ) -> str:
        return self.build_prompt_payload(
            user_prompt,
            engine,
            incremental=incremental,
            extra_sections=extra_sections,
        )["final_prompt"]

    def build_prompt_payload(
        self,
        user_prompt: str,
        engine: str,
        incremental: bool = False,
        extra_sections: list[tuple[str, str]] | None = None,
        inject_memory: bool = True,
    ) -> dict[str, Any]:
        """Build the prompt to send to the backend.

        Args:
            incremental: When True (fixed/session mode), skip system identity,
                recent turns, and memories — the CLI session already has them.
                Only include /sys slots, active skills, and the user prompt.
            inject_memory: When False, skip recent turns and memory retrieval
                even if the per-section injection flags are enabled. Used by
                /new and /fresh to ensure a clean first prompt.
        """
        # Per-engine memory injection limits — smaller models get less context
        _memory_limits = {
            "ollama-api":      {"recent_turns": 4, "memories": 2},
            "deepseek-api":    {"recent_turns": 8, "memories": 4},
            "openrouter-api":  {"recent_turns": 10, "memories": 6},
            "claude-cli":      {"recent_turns": 10, "memories": 6},
            "gemini-cli":      {"recent_turns": 8, "memories": 4},
            "codex-cli":       {"recent_turns": 8, "memories": 4},
        }
        limits = _memory_limits.get(engine, {"recent_turns": 10, "memories": 6})

        system_text = "" if incremental else self._load_system_prompt()
        inject_base = not incremental and inject_memory
        inject_turns = inject_base and self.turns_injection_enabled
        inject_saved_memory = inject_base and self.saved_memory_injection_enabled
        recent_turns = self.memory_store.get_recent_turns(limit=limits["recent_turns"]) if inject_turns else []
        memories = self.memory_store.retrieve_memories(user_prompt, limit=limits["memories"]) if inject_saved_memory else []
        active_skills = []
        if callable(self.active_skill_provider):
            try:
                active_skills = list(self.active_skill_provider() or [])
            except Exception:
                active_skills = []

        section_blocks: list[dict[str, Any]] = []

        def add_section(key: str, title: str, body_parts: list[str], item_count: int = 0) -> None:
            parts = [part for part in body_parts if part]
            if not parts:
                return
            block = f"--- {title} ---\n\n" + "\n\n".join(parts)
            section_blocks.append(
                {
                    "key": key,
                    "title": title,
                    "text": block,
                    "chars": len(block),
                    "tokens_est": _estimate_tokens(block),
                    "item_count": item_count,
                }
            )

        if self.sys_prompt_manager:
            active_sys = self.sys_prompt_manager.get_active_texts()
            if active_sys:
                add_section(
                    "additional_system_context",
                    "ADDITIONAL SYSTEM CONTEXT",
                    list(active_sys),
                    item_count=len(active_sys),
                )

        if system_text:
            add_section("system_identity", "SYSTEM IDENTITY", [system_text], item_count=1)

        if active_skills:
            skill_parts = []
            for skill_id, skill_name, skill_body in active_skills:
                skill_parts.append(f"## [{skill_id}] {skill_name}")
                skill_parts.append(skill_body)
            add_section("active_skills", "ACTIVE SKILLS", skill_parts, item_count=len(active_skills))

        if extra_sections:
            for title, body in extra_sections:
                if not title or not body:
                    continue
                add_section(f"extra:{title.lower().replace(' ', '_')}", title, [body], item_count=1)

        if memories:
            memory_parts = []
            for m in memories:
                memory_parts.append(f"[{m['memory_type']}/{m['source']}] {m['content']}")
            add_section(
                "relevant_long_term_memory",
                "RELEVANT LONG-TERM MEMORY",
                memory_parts,
                item_count=len(memories),
            )

        if recent_turns:
            recent_parts = []
            for t in recent_turns:
                recent_parts.append(f"{t['role'].upper()}: {t['text']}")
            add_section("recent_context", "RECENT CONTEXT", recent_parts, item_count=len(recent_turns))

        if not section_blocks:
            return {
                "final_prompt": user_prompt,
                "audit": {
                    "incremental": incremental,
                    "budget_limit_chars": self.PROMPT_BUDGETS.get(engine, 30000),
                    "budget_applied": False,
                    "context_chars_before_budget": 0,
                    "final_prompt_chars_before_budget": len(user_prompt),
                    "final_prompt_chars_after_budget": len(user_prompt),
                    "time_fyi_chars": 0,
                    "context_fingerprint": "",
                    "sections": [],
                },
            }

        time_fyi = self._build_time_fyi()
        context_text = "\n\n".join(section["text"] for section in section_blocks)
        final_prompt_unbudgeted = (
            "Bridge-managed context follows. Use it as background memory. "
            "Respond only to NEW REQUEST unless explicitly asked to summarize memory.\n\n"
            + context_text
            + "\n\n--- NEW REQUEST ---\n"
            + time_fyi + "\n\n"
            + user_prompt
        )
        final_prompt = self._apply_budget(final_prompt_unbudgeted, engine)
        return {
            "final_prompt": final_prompt,
            "audit": {
                "incremental": incremental,
                "budget_limit_chars": self.PROMPT_BUDGETS.get(engine, 30000),
                "budget_applied": final_prompt != final_prompt_unbudgeted,
                "context_chars_before_budget": len(context_text),
                "final_prompt_chars_before_budget": len(final_prompt_unbudgeted),
                "final_prompt_chars_after_budget": len(final_prompt),
                "time_fyi_chars": len(time_fyi),
                "context_fingerprint": hashlib.sha1(context_text.encode("utf-8")).hexdigest()[:16],
                "sections": [
                    {
                        "key": section["key"],
                        "title": section["title"],
                        "chars": section["chars"],
                        "tokens_est": section["tokens_est"],
                        "item_count": section["item_count"],
                    }
                    for section in section_blocks
                ],
            },
        }
