from __future__ import annotations
import json
import math
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


class LocalEmbeddingEncoder:
    """Dependency-free hashed embedding encoder for durable local retrieval."""

    def __init__(self, dim: int = 256):
        self.dim = dim

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


class BridgeMemoryStore:
    def __init__(self, workspace_dir: Path):
        self.workspace_dir = workspace_dir
        self.db_path = workspace_dir / "bridge_memory.sqlite"
        self.encoder = LocalEmbeddingEncoder()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

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
            conn.commit()

    def _now(self) -> str:
        return datetime.now().isoformat()

    def _safe_query(self, query: str) -> str:
        q = (query or "").replace('"', '""').strip()
        if not q:
            return ""
        parts = [p for p in re.findall(r"[a-zA-Z0-9_]+", q) if len(p) > 1]
        if not parts:
            return ""
        return " OR ".join(parts[:16])

    def record_turn(self, role: str, source: str, text: str):
        clean = (text or "").strip()
        if not clean:
            return
        emb = json.dumps(self.encoder.encode(clean))
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO turns (ts, role, source, text, embedding) VALUES (?, ?, ?, ?, ?)",
                (self._now(), role, source, clean, emb),
            )
            conn.commit()

    def record_exchange(self, user_text: str, assistant_text: str, source: str):
        user_clean = (user_text or "").strip()
        assistant_clean = (assistant_text or "").strip()
        if not user_clean or not assistant_clean:
            return
        episode = f"User: {user_clean}\nAssistant: {assistant_clean}"
        emb = json.dumps(self.encoder.encode(episode))
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO memories (ts, memory_type, source, content, importance, embedding)
                VALUES (?, 'episodic', ?, ?, ?, ?)
                """,
                (self._now(), source, episode, 1.0, emb),
            )
            memory_id = cur.lastrowid
            conn.execute(
                "INSERT INTO memory_fts (content, memory_id, source) VALUES (?, ?, ?)",
                (episode, memory_id, source),
            )
            conn.commit()

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
        candidates: dict[int, dict[str, Any]] = {}

        with self._connect() as conn:
            if safe_query:
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
            try:
                emb = json.loads(row["embedding"])
            except Exception:
                emb = []
            sim = self.encoder.cosine(q_vec, emb)
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
    }

    def __init__(self, memory_store: BridgeMemoryStore, system_md: Path | None, active_skill_provider=None, sys_prompt_manager=None):
        self.memory_store = memory_store
        self.system_md = system_md
        self.active_skill_provider = active_skill_provider
        self.sys_prompt_manager = sys_prompt_manager

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

    def build_prompt(self, user_prompt: str, engine: str, incremental: bool = False) -> str:
        """Build the prompt to send to the backend.

        Args:
            incremental: When True (fixed/session mode), skip system identity,
                recent turns, and memories — the CLI session already has them.
                Only include /sys slots, active skills, and the user prompt.
        """
        system_text = "" if incremental else self._load_system_prompt()
        recent_turns = [] if incremental else self.memory_store.get_recent_turns(limit=10)
        memories = [] if incremental else self.memory_store.retrieve_memories(user_prompt, limit=6)
        active_skills = []
        if callable(self.active_skill_provider):
            try:
                active_skills = list(self.active_skill_provider() or [])
            except Exception:
                active_skills = []

        context_parts = []
        if self.sys_prompt_manager:
            active_sys = self.sys_prompt_manager.get_active_texts()
            if active_sys:
                context_parts.append("--- ADDITIONAL SYSTEM CONTEXT ---")
                for txt in active_sys:
                    context_parts.append(txt)

        if system_text:
            context_parts.append("--- SYSTEM IDENTITY ---")
            context_parts.append(system_text)

        if active_skills:
            context_parts.append("--- ACTIVE SKILLS ---")
            for skill_id, skill_name, skill_body in active_skills:
                context_parts.append(f"## [{skill_id}] {skill_name}")
                context_parts.append(skill_body)

        if memories:
            context_parts.append("--- RELEVANT LONG-TERM MEMORY ---")
            for m in memories:
                context_parts.append(f"[{m['memory_type']}/{m['source']}] {m['content']}")

        if recent_turns:
            context_parts.append("--- RECENT CONTEXT ---")
            for t in recent_turns:
                context_parts.append(f"{t['role'].upper()}: {t['text']}")

        if not context_parts:
            return user_prompt

        time_fyi = self._build_time_fyi()
        final_prompt = (
            "Bridge-managed context follows. Use it as background memory. "
            "Respond only to NEW REQUEST unless explicitly asked to summarize memory.\n\n"
            + "\n\n".join(context_parts)
            + "\n\n--- NEW REQUEST ---\n"
            + time_fyi + "\n\n"
            + user_prompt
        )
        return self._apply_budget(final_prompt, engine)
