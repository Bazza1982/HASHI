#!/usr/bin/env python3
"""
Dream — Nightly AI reflection and long-term memory consolidation.

Usage: dream.py [on|off|now|run|undo|status]
  on     — enable nightly dream cron (01:30)
  off    — disable nightly dream cron
  now    — run dream reflection immediately (user-triggered)
  run    — run dream reflection (called by scheduler, same as now)
  undo   — restore snapshot from last dream (no LLM)
  status — show current dream state

Environment (set by bridge skill runner):
  BRIDGE_PROJECT_ROOT  — path to hashi project root
  BRIDGE_WORKSPACE_DIR — path to this agent's workspace
  BRIDGE_SKILL_ID      — "dream"
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ─── paths ───────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(os.environ.get("BRIDGE_PROJECT_ROOT", Path(__file__).parent.parent.parent))
WORKSPACE_DIR = Path(os.environ.get("BRIDGE_WORKSPACE_DIR", PROJECT_ROOT / "workspaces" / "lily"))
AGENT_NAME = WORKSPACE_DIR.name
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from orchestrator.bridge_memory import BridgeMemoryStore
from orchestrator.config import AgentConfig, ConfigManager, FlexibleAgentConfig
from orchestrator.flexible_backend_registry import get_secret_lookup_order
from orchestrator.habits import HabitStore
from adapters.registry import get_backend_class

TASKS_PATH = PROJECT_ROOT / "tasks.json"
AGENTS_PATH = PROJECT_ROOT / "agents.json"
SECRETS_PATH = PROJECT_ROOT / "secrets.json"
STATE_PATH = WORKSPACE_DIR / "state.json"
TRANSCRIPT_PATH = WORKSPACE_DIR / "transcript.jsonl"
AGENT_MD_PATH = WORKSPACE_DIR / "AGENT.md"
DREAM_DIR = WORKSPACE_DIR / "dream_snapshots"
DREAM_LOG_PATH = WORKSPACE_DIR / "dream_log.md"
MEMORY_DB_PATH = WORKSPACE_DIR / "bridge_memory.sqlite"

CRON_JOB_ID = f"dream-{AGENT_NAME}-nightly"
CRON_TIME = "01:30"

MAX_NEW_MEMORIES = 20          # soft safety cap, not a creative constraint
MIN_IMPORTANCE = 0.7
MAX_AGENT_MD_SECTIONS = 3
SNAPSHOT_RETENTION_DAYS = 7
MEMORY_FORGET_THRESHOLD = 200   # start forgetting when agent exceeds this many memories
MEMORY_FORGET_TARGET = 160      # trim down to this count when threshold exceeded
MAX_FORGET_PER_DREAM = 20       # safety cap — never delete more than this in one run
_MEMORY_STORE: BridgeMemoryStore | None = None
_HABIT_STORE: HabitStore | None = None
SUPPORTED_SAFE_BACKENDS = {"claude-cli", "codex-cli", "gemini-cli"}
_CONFIG_CACHE: tuple | None = None


# ─── helpers ─────────────────────────────────────────────────────────────────

def _read_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_tasks() -> dict:
    try:
        return json.loads(TASKS_PATH.read_text())
    except Exception:
        return {"version": 1, "heartbeats": [], "crons": []}


def _read_agents() -> dict:
    try:
        return json.loads(AGENTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"agents": []}


def _write_tasks(tasks: dict):
    TASKS_PATH.write_text(json.dumps(tasks, indent=2, ensure_ascii=False))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _resolve_backend_context() -> tuple[str | None, str | None]:
    env_backend = (os.environ.get("BRIDGE_ACTIVE_BACKEND") or "").strip()
    env_model = (os.environ.get("BRIDGE_ACTIVE_MODEL") or "").strip()
    if env_backend and env_model:
        return env_backend, env_model

    state = _read_state()
    backend = str(state.get("active_backend") or "").strip() or None
    model = str(state.get("active_model") or "").strip() or None
    if backend and model:
        return backend, model

    agents = _read_agents().get("agents", [])
    for entry in agents:
        if str(entry.get("name") or "").strip().lower() != AGENT_NAME.lower():
            continue
        backend = str(entry.get("active_backend") or entry.get("engine") or "").strip() or None
        if entry.get("type") == "flex":
            allowed = entry.get("allowed_backends") or []
            for option in allowed:
                if str(option.get("engine") or "").strip() == backend:
                    model = str(option.get("model") or "").strip() or None
                    break
        if not model:
            model = str(entry.get("model") or "").strip() or None
        return backend, model
    return None, None


def _messages_to_prompt(messages: list[dict]) -> str:
    system_parts: list[str] = []
    history_parts: list[str] = []

    for msg in messages:
        role = str(msg.get("role") or "").lower()
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = " ".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        content = str(content).strip()
        if not content:
            continue
        if role == "system":
            system_parts.append(content)
        elif role == "user":
            history_parts.append(f"User: {content}")
        elif role == "assistant":
            history_parts.append(f"Assistant: {content}")

    parts: list[str] = []
    if system_parts:
        parts.append("\n\n".join(system_parts))
    if history_parts:
        parts.append("\n".join(history_parts))
    return "\n\n".join(parts)


def _load_runtime_config():
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE

    manager = ConfigManager(AGENTS_PATH, SECRETS_PATH, bridge_home=PROJECT_ROOT)
    global_cfg, agents, secrets = manager.load()
    agent_cfg = next(
        (entry for entry in agents if str(getattr(entry, "name", "")).lower() == AGENT_NAME.lower()),
        None,
    )
    if agent_cfg is None:
        raise RuntimeError(f"Dream could not find agent config for '{AGENT_NAME}'.")
    _CONFIG_CACHE = (global_cfg, agent_cfg, secrets)
    return _CONFIG_CACHE


def _resolve_api_key_direct(engine: str, secrets: dict) -> str | None:
    for secret_key in get_secret_lookup_order(engine, AGENT_NAME):
        api_key = secrets.get(secret_key)
        if api_key:
            return api_key
    return None


def _build_direct_adapter_config(
    engine: str,
    model: str,
    loaded_agent_cfg: FlexibleAgentConfig | AgentConfig,
):
    if isinstance(loaded_agent_cfg, FlexibleAgentConfig):
        backend_cfg_raw = next((b for b in loaded_agent_cfg.allowed_backends if b["engine"] == engine), None)
        if not backend_cfg_raw:
            raise RuntimeError(f"Dream could not find backend '{engine}' in allowed_backends for '{AGENT_NAME}'.")
        agent_extra = dict(getattr(loaded_agent_cfg, "extra", None) or {})
        backend_extra = dict(backend_cfg_raw)
        backend_extra.pop("engine", None)
        backend_extra.pop("model", None)
        backend_scope = backend_cfg_raw.get("access_scope", loaded_agent_cfg.access_scope)
        backend_extra.pop("access_scope", None)
        extra = {**agent_extra, **backend_extra}
        return AgentConfig(
            name=loaded_agent_cfg.name,
            engine=engine,
            workspace_dir=loaded_agent_cfg.workspace_dir,
            system_md=loaded_agent_cfg.system_md,
            model=model,
            is_active=True,
            extra=extra,
            access_scope=backend_scope,
            project_root=loaded_agent_cfg.project_root,
        )

    extra = dict(getattr(loaded_agent_cfg, "extra", None) or {})
    return AgentConfig(
        name=loaded_agent_cfg.name,
        engine=engine,
        workspace_dir=loaded_agent_cfg.workspace_dir,
        system_md=loaded_agent_cfg.system_md,
        model=model,
        is_active=True,
        resume_policy=getattr(loaded_agent_cfg, "resume_policy", "latest"),
        extra=extra,
        access_scope=loaded_agent_cfg.access_scope,
        project_root=loaded_agent_cfg.project_root,
    )


async def _call_current_backend_direct(prompt: str, backend: str, model: str) -> str:
    global_cfg, loaded_agent_cfg, secrets = _load_runtime_config()
    adapter_cfg = _build_direct_adapter_config(backend, model, loaded_agent_cfg)
    backend_class = get_backend_class(backend)
    api_key = _resolve_api_key_direct(backend, secrets)
    adapter = backend_class(adapter_cfg, global_cfg, api_key)

    if not await adapter.initialize():
        raise RuntimeError(f"Dream failed to initialize backend '{backend}'.")

    request_id = f"dream-{AGENT_NAME}-{int(time.time() * 1000)}"
    try:
        response = await adapter.generate_response(prompt, request_id, silent=True)
    finally:
        await adapter.shutdown()

    if not response.is_success:
        raise RuntimeError(response.error or f"Dream backend '{backend}' returned failure.")
    return response.text


def _call_current_backend(_backend_hint: str | None, messages: list[dict], model: str | None = None) -> str:
    backend, resolved_model = _resolve_backend_context()
    if not backend or not resolved_model:
        raise RuntimeError("Dream could not resolve the agent's current backend/model.")
    if backend not in SUPPORTED_SAFE_BACKENDS:
        raise RuntimeError(
            f"Dream refuses unsafe backend '{backend}'. Allowed backends: {', '.join(sorted(SUPPORTED_SAFE_BACKENDS))}."
        )
    prompt = _messages_to_prompt(messages)
    target_model = model or resolved_model
    try:
        return asyncio.run(_call_current_backend_direct(prompt, backend, target_model))
    except Exception as exc:
        raise RuntimeError(f"Current-backend direct call failed: {exc}") from exc


def _load_transcript_for_date(target_date: str) -> list[dict]:
    """Load user+assistant turns from transcript.jsonl for a specific local date.

    For entries WITH a 'ts' field: included only if the local date matches target_date.
    For legacy entries WITHOUT 'ts': included only if they appear between or after
    entries that match target_date (i.e. once we've seen at least one matching entry).

    If NO entries have a 'ts' at all (fully legacy transcript), falls back to returning
    the last 60 entries — the old behavior — so dream still works for agents that
    predate the ts addition.
    """
    if not TRANSCRIPT_PATH.exists():
        return []
    all_entries: list[dict] = []
    any_has_ts = False
    try:
        for line in TRANSCRIPT_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("role") not in ("user", "assistant"):
                continue
            if entry.get("ts"):
                any_has_ts = True
            all_entries.append(entry)
    except Exception:
        return []

    # Fully legacy transcript (no ts fields at all) — return tail entries
    if not any_has_ts:
        return all_entries[-60:]

    # Resolve local dates for all entries (None for undated)
    resolved: list[tuple[dict, str | None]] = []
    for entry in all_entries:
        ts = entry.get("ts", "")
        local_date = None
        if ts:
            try:
                utc_dt = datetime.fromisoformat(ts)
                local_date = utc_dt.astimezone().strftime("%Y-%m-%d")
            except (ValueError, OSError):
                pass
        resolved.append((entry, local_date))

    # Find the last dated entry's local_date — if it's the day BEFORE target_date,
    # then undated entries trailing after it are likely today's activity (partial-legacy).
    last_dated_date = None
    last_dated_idx = -1
    for i in range(len(resolved) - 1, -1, -1):
        if resolved[i][1] is not None:
            last_dated_date = resolved[i][1]
            last_dated_idx = i
            break

    # Collect entries matching target_date
    turns: list[dict] = []
    seen_target = False
    for entry, local_date in resolved:
        if local_date == target_date:
            seen_target = True
            turns.append(entry)
        elif local_date is not None:
            # Different date — if we were collecting, stop (passed the window)
            if seen_target:
                break
        elif seen_target:
            # Undated entry interleaved with matching block — include
            turns.append(entry)

    # Partial-legacy fix: if no dated entries matched target_date, but there are
    # undated entries trailing AFTER the last dated block (which is from an earlier
    # date), those undated entries are the most recent activity — include them.
    if not turns and last_dated_date is not None and last_dated_date < target_date:
        for entry, local_date in resolved[last_dated_idx + 1:]:
            if local_date is None:
                turns.append(entry)
            else:
                break  # hit another dated block

    return turns


def _memory_store() -> BridgeMemoryStore:
    global _MEMORY_STORE
    if _MEMORY_STORE is None:
        _MEMORY_STORE = BridgeMemoryStore(WORKSPACE_DIR)
    return _MEMORY_STORE


def _resolve_agent_class() -> str:
    agents = _read_agents().get("agents", [])
    for entry in agents:
        if str(entry.get("name") or entry.get("id") or "").strip().lower() != AGENT_NAME.lower():
            continue
        extra = entry.get("extra") or {}
        return str(entry.get("agent_class") or extra.get("agent_class") or "general").strip().lower()
    return "general"


def _habit_store() -> HabitStore:
    global _HABIT_STORE
    if _HABIT_STORE is None:
        _HABIT_STORE = HabitStore(
            workspace_dir=WORKSPACE_DIR,
            project_root=PROJECT_ROOT,
            agent_id=AGENT_NAME,
            agent_class=_resolve_agent_class(),
        )
    return _HABIT_STORE


def _write_memory(content: str, memory_type: str, source: str, importance: float) -> int | None:
    """Write a single memory entry through the canonical bridge store."""
    return _memory_store().record_memory(
        memory_type=memory_type,
        source=source,
        content=content,
        importance=importance,
    )


def _count_memories() -> int:
    if not MEMORY_DB_PATH.exists():
        return 0
    try:
        with sqlite3.connect(str(MEMORY_DB_PATH)) as conn:
            row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
            return row[0] if row else 0
    except Exception:
        return 0


def _fetch_low_importance_memories(limit: int) -> list[dict]:
    """Fetch memories sorted by importance ASC, last_accessed ASC for forgetting candidates."""
    if not MEMORY_DB_PATH.exists():
        return []
    try:
        with sqlite3.connect(str(MEMORY_DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT id, content, memory_type, importance, created_at, last_accessed
                   FROM memories
                   ORDER BY importance ASC, last_accessed ASC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def _forget_memories(current_count: int) -> tuple[int, list[str]]:
    """Ask LLM which memories to forget, delete them, return (count_deleted, summaries)."""
    candidates = _fetch_low_importance_memories(limit=min(60, current_count))
    if not candidates:
        return 0, []

    candidate_text = "\n".join(
        f"[ID:{m['id']} importance:{m['importance']:.2f} accessed:{m.get('last_accessed','?')[:10]}] {m['content'][:200]}"
        for m in candidates
    )
    n_to_forget = min(MAX_FORGET_PER_DREAM, current_count - MEMORY_FORGET_TARGET)

    forget_prompt = f"""You are managing long-term memory for an AI assistant. The memory store has {current_count} entries, which exceeds the healthy threshold of {MEMORY_FORGET_THRESHOLD}.

Your task: select up to {n_to_forget} memories to FORGET — permanently delete.

Choose memories that are:
- Low importance (score < 0.75)
- Old and not recently accessed
- Redundant, superseded, or too trivial to matter in future conversations

Do NOT forget:
- Memories about user preferences, identity, or long-term goals
- Memories with importance >= 0.8
- Memories accessed recently

CANDIDATE MEMORIES (lowest importance first):
{candidate_text}

Respond ONLY with valid JSON:
{{
  "forget_ids": [list of integer IDs to delete],
  "reason": "One-line explanation of selection criteria"
}}

If nothing should be forgotten, return {{"forget_ids": [], "reason": "..."}}
    """

    try:
        response = _call_current_backend(None, [{"role": "user", "content": forget_prompt}])
        clean = response.strip()
        if clean.startswith("```"):
            clean = "\n".join(clean.split("\n")[1:])
            if clean.endswith("```"):
                clean = clean[:-3]
        result = json.loads(clean.strip())
        forget_ids = [int(i) for i in result.get("forget_ids", [])][:n_to_forget]
        reason = result.get("reason", "")
    except Exception:
        return 0, []

    if not forget_ids:
        return 0, []

    # build summaries before deletion
    id_to_content = {m["id"]: m["content"][:80] for m in candidates}
    summaries = [id_to_content.get(i, f"ID:{i}") for i in forget_ids if i in id_to_content]

    _delete_memories_by_ids(forget_ids)
    return len(forget_ids), summaries


def _delete_memories_by_ids(ids: list[int]):
    if not ids or not MEMORY_DB_PATH.exists():
        return
    with sqlite3.connect(str(MEMORY_DB_PATH)) as conn:
        try:
            import sqlite_vec

            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except Exception:
            pass
        placeholders = ",".join("?" * len(ids))
        try:
            conn.execute(f"DELETE FROM memory_vec WHERE memory_id IN ({placeholders})", ids)
        except Exception:
            pass
        conn.execute(f"DELETE FROM memory_fts WHERE memory_id IN ({placeholders})", ids)
        conn.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", ids)
        conn.commit()


# ─── snapshot ─────────────────────────────────────────────────────────────────

def _compute_turns_hash(turns: list[dict]) -> str:
    """Compute a content hash of transcript turns to detect stale/unchanged content."""
    content = "".join(f"{t.get('role','')}:{t.get('text','')}" for t in turns)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _get_latest_snapshot_path() -> Path | None:
    """Return the path to the most recent dream snapshot, or None."""
    if not DREAM_DIR.exists():
        return None
    snapshots = sorted(DREAM_DIR.glob("dream_*.json"), reverse=True)
    return snapshots[0] if snapshots else None


def _get_last_dream_turns_hash() -> str | None:
    """Read the turns_hash from the most recent dream snapshot."""
    snap_path = _get_latest_snapshot_path()
    if not snap_path:
        return None
    try:
        snap = json.loads(snap_path.read_text(encoding="utf-8"))
        return snap.get("turns_hash")
    except Exception:
        return None


def _save_snapshot(added_memory_ids: list[int], agent_md_before: str | None, dream_date: str | None = None, turns_hash: str | None = None) -> Path:
    DREAM_DIR.mkdir(exist_ok=True)
    date_label = dream_date or _today_str()
    snapshot = {
        "date": date_label,
        "ts": _now_iso(),
        "agent_md_before": agent_md_before,
        "added_memory_ids": added_memory_ids,
        "turns_hash": turns_hash,
    }
    path = DREAM_DIR / f"dream_{date_label}.json"
    path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False))
    # purge old snapshots
    cutoff = datetime.now().timestamp() - SNAPSHOT_RETENTION_DAYS * 86400
    for old in DREAM_DIR.glob("dream_*.json"):
        if old.stat().st_mtime < cutoff:
            old.unlink(missing_ok=True)
    return path


def _load_latest_snapshot() -> dict | None:
    if not DREAM_DIR.exists():
        return None
    snapshots = sorted(DREAM_DIR.glob("dream_*.json"), reverse=True)
    if not snapshots:
        return None
    try:
        return json.loads(snapshots[0].read_text())
    except Exception:
        return None


# ─── dream log ────────────────────────────────────────────────────────────────

def _append_dream_log(date: str, summary: str, memory_count: int, agent_md_updated: bool):
    entry = f"\n## {date}\n\n{summary}\n\n_Memories added: {memory_count} | Agent.md updated: {'yes' if agent_md_updated else 'no'}_\n"
    with open(DREAM_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(entry)


# ─── commands ─────────────────────────────────────────────────────────────────

def cmd_on() -> str:
    tasks = _read_tasks()
    crons = tasks.setdefault("crons", [])
    existing = next((c for c in crons if c["id"] == CRON_JOB_ID), None)
    if existing:
        if existing.get("enabled"):
            return f"✅ Dream is already enabled for {AGENT_NAME} at {CRON_TIME}."
        existing["enabled"] = True
        _write_tasks(tasks)
        return f"🌙 Dream re-enabled for {AGENT_NAME} — nightly reflection at {CRON_TIME}."
    crons.append({
        "id": CRON_JOB_ID,
        "agent": AGENT_NAME,
        "enabled": True,
        "time": CRON_TIME,
        "action": "skill:dream",
        "args": "run",
        "note": f"[Dream] Nightly reflection for {AGENT_NAME}",
    })
    _write_tasks(tasks)
    return f"🌙 Dream enabled for {AGENT_NAME} — nightly reflection scheduled at {CRON_TIME}."


def cmd_off() -> str:
    tasks = _read_tasks()
    crons = tasks.get("crons", [])
    existing = next((c for c in crons if c["id"] == CRON_JOB_ID), None)
    if not existing:
        return f"Dream is not configured for {AGENT_NAME}."
    if not existing.get("enabled"):
        return f"Dream is already disabled for {AGENT_NAME}."
    existing["enabled"] = False
    _write_tasks(tasks)
    return f"🌙 Dream disabled for {AGENT_NAME}. Use '/skill dream on' to re-enable."


def cmd_status() -> str:
    tasks = _read_tasks()
    cron = next((c for c in tasks.get("crons", []) if c["id"] == CRON_JOB_ID), None)
    if not cron:
        status_line = "🔴 Not configured"
    elif cron.get("enabled"):
        status_line = f"🟢 Enabled — runs nightly at {CRON_TIME}"
    else:
        status_line = "🟡 Disabled"

    snapshot = _load_latest_snapshot()
    if snapshot:
        last_date = snapshot.get("date", "unknown")
        mem_count = len(snapshot.get("added_memory_ids", []))
        snap_line = f"Last dream: {last_date} ({mem_count} memories added)"
    else:
        snap_line = "No dream snapshots found"

    log_exists = "✅" if DREAM_LOG_PATH.exists() else "—"
    mem_count = _count_memories()
    mem_status = f"{mem_count} memories"
    if mem_count > MEMORY_FORGET_THRESHOLD:
        mem_status += f" ⚠️ (>{MEMORY_FORGET_THRESHOLD} — forgetting will run next dream)"

    lines = [
        f"🌙 Dream Status — {AGENT_NAME}",
        "",
        f"Schedule: {status_line}",
        f"Snapshot: {snap_line}",
        f"Memory store: {mem_status}",
        f"Dream log: {log_exists}",
        "",
        f"Forget threshold: {MEMORY_FORGET_THRESHOLD} | Target: {MEMORY_FORGET_TARGET} | Max per run: {MAX_FORGET_PER_DREAM}",
        "",
        "Commands: on | off | now | undo | status",
    ]
    return "\n".join(lines)


def cmd_now(is_scheduled: bool = False) -> str:
    """Run the dream reflection process."""
    backend, model = _resolve_backend_context()
    if not backend or not model:
        return "❌ Dream: Could not resolve the agent's current backend/model."
    if backend not in SUPPORTED_SAFE_BACKENDS:
        return (
            f"❌ Dream: Current backend '{backend}' is not permitted for dream. "
            f"Allowed backends: {', '.join(sorted(SUPPORTED_SAFE_BACKENDS))}."
        )

    # Scheduled runs (cron at 01:30) reflect on yesterday; manual runs reflect on today.
    # If today has no turns, also try yesterday as fallback (for manual runs shortly after midnight).
    today = _today_str()
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    if is_scheduled:
        dream_date = yesterday
        turns = _load_transcript_for_date(yesterday)
    else:
        turns = _load_transcript_for_date(today)
        dream_date = today
        if len(turns) < 4:
            # Fallback: try yesterday (user might run /dream now shortly after midnight)
            turns = _load_transcript_for_date(yesterday)
            dream_date = yesterday
    if len(turns) < 4:
        return (
            f"🌙 Dream skipped — not enough conversation on {dream_date} to reflect on "
            f"(found {len(turns)} turns, need at least 4)."
        )

    # Stale content gate 1: skip if transcript file hasn't been modified since last dream snapshot
    last_snapshot = _get_latest_snapshot_path()
    if last_snapshot and TRANSCRIPT_PATH.exists():
        transcript_mtime = TRANSCRIPT_PATH.stat().st_mtime
        snapshot_mtime = last_snapshot.stat().st_mtime
        if transcript_mtime < snapshot_mtime:
            return (
                f"🌙 Dream skipped — no new activity since last dream on {dream_date} "
                f"(transcript not modified since last dream)."
            )

    # Stale content gate 2: skip if transcript content hash is identical to last dream
    current_hash = _compute_turns_hash(turns)
    last_hash = _get_last_dream_turns_hash()
    if current_hash == last_hash:
        return (
            f"🌙 Dream skipped — no new activity since last dream on {dream_date} "
            f"(transcript unchanged, hash={current_hash[:8]})."
        )

    # Build transcript excerpt (cap at ~12000 chars to give LLM enough context)
    transcript_text = ""
    for t in turns[-80:]:
        role = "User" if t["role"] == "user" else "Assistant"
        transcript_text += f"{role}: {t['text'][:1000]}\n\n"
    if len(transcript_text) > 12000:
        transcript_text = transcript_text[-12000:]

    agent_md_content = AGENT_MD_PATH.read_text(encoding="utf-8") if AGENT_MD_PATH.exists() else ""

    # Build dream prompt
    dream_prompt = f"""You are performing a nightly memory consolidation for an AI assistant agent named {AGENT_NAME}.

Review the conversation transcript from {dream_date} and decide what is truly worth remembering long-term to serve the user better.

Think like a human brain during sleep: extract the essence, compress the important, discard the noise.

TRANSCRIPT FROM {dream_date}:
---
{transcript_text}
---

CURRENT AGENT.MD (first 2000 chars):
---
{agent_md_content[:2000]}
---

Respond ONLY with valid JSON in this exact format:
{{
  "new_memories": [
    {{
      "content": "Memory text — detail level should match importance",
      "memory_type": "episodic",
      "importance": 0.8,
      "source": "dream:{dream_date}"
    }}
  ],
  "agent_md_updates": [
    {{
      "section": "Section name or heading to update",
      "reason": "Why this update matters",
      "new_content": "The exact new content for this section"
    }}
  ],
  "reflection_summary": "2-4 sentence summary of what was learned on {dream_date} and why it matters for future interactions. Write in first person as the agent."
}}

RULES (follow strictly):
1. There is NO fixed quota on memories. Output as many as genuinely warranted — 0 if nothing significant, 15 if it was a rich day. Let the conversation guide you.
2. IMPORTANCE SCORING — the strongest signal is how much time/turns the user spent on a topic:
   - Long back-and-forth debugging, deep discussion, multi-step work → 0.9-1.0
   - Medium conversation, decisions made, clear outcomes → 0.8-0.9
   - Brief mention, minor preference, small fact → 0.7-0.8
   - Trivial chit-chat, greetings, noise → skip entirely (below {MIN_IMPORTANCE})
3. DETAIL MATCHES IMPORTANCE — importance controls how much you write:
   - 0.9-1.0: Full context — what happened, why, what was decided, key details, consequences. 2-5 sentences.
   - 0.8-0.9: Clear summary with key decision or outcome. 1-2 sentences.
   - 0.7-0.8: One-liner fact or preference.
4. agent_md_updates: max {MAX_AGENT_MD_SECTIONS} sections, only for genuinely significant insights
5. Do NOT invent facts — only extract from the actual transcript
6. Do NOT update agent.md for trivial things; only for meaningful behavioral insights
7. If nothing significant happened, return empty arrays and say so in reflection_summary
"""

    try:
        response = _call_current_backend(backend, [{"role": "user", "content": dream_prompt}], model=model)
    except RuntimeError as e:
        return f"❌ Dream: backend call failed — {e}"

    # Parse JSON response
    try:
        # Strip markdown code blocks if present
        clean = response.strip()
        if clean.startswith("```"):
            clean = "\n".join(clean.split("\n")[1:])
            if clean.endswith("```"):
                clean = clean[:-3]
        clean = clean.strip()
        # Fix truncated JSON: if response was cut off, try to salvage what we have
        dream_result = None
        try:
            dream_result = json.loads(clean)
        except json.JSONDecodeError:
            # Try to find the last complete JSON object
            import re
            # Extract each complete memory object and build a partial result
            mem_pattern = re.compile(r'\{[^{}]*"content"\s*:\s*"[^"]*"[^{}]*\}')
            memories = []
            for m in mem_pattern.finditer(clean):
                try:
                    memories.append(json.loads(m.group()))
                except json.JSONDecodeError:
                    continue
            # Extract reflection_summary if present
            summary_match = re.search(r'"reflection_summary"\s*:\s*"([^"]*)"', clean)
            summary = summary_match.group(1) if summary_match else "Partial dream — response was truncated."
            if memories:
                dream_result = {
                    "new_memories": memories,
                    "agent_md_updates": [],
                    "reflection_summary": summary,
                }
        if dream_result is None:
            raise json.JSONDecodeError("Could not parse or salvage JSON", clean, 0)
    except json.JSONDecodeError as e:
        return f"❌ Dream: Failed to parse LLM response as JSON — {e}\n\nRaw response:\n{response[:500]}"

    new_memories: list[dict] = dream_result.get("new_memories", [])
    agent_md_updates: list[dict] = dream_result.get("agent_md_updates", [])
    reflection_summary: str = dream_result.get("reflection_summary", "No summary provided.")

    # Filter memories by importance threshold (safety cap at MAX_NEW_MEMORIES)
    valid_memories = [
        m for m in new_memories
        if isinstance(m, dict) and float(m.get("importance", 0)) >= MIN_IMPORTANCE
    ]
    if len(valid_memories) > MAX_NEW_MEMORIES:
        # Sort by importance desc, keep the top ones
        valid_memories.sort(key=lambda m: float(m.get("importance", 0)), reverse=True)
        valid_memories = valid_memories[:MAX_NEW_MEMORIES]

    # ── Forgetting phase (before adding new memories) ──────────────────────────
    current_memory_count = _count_memories()
    forgotten_count = 0
    forgotten_summaries: list[str] = []
    if current_memory_count > MEMORY_FORGET_THRESHOLD:
        forgotten_count, forgotten_summaries = _forget_memories(current_memory_count)

    # Save snapshot BEFORE applying changes
    agent_md_before = agent_md_content if agent_md_updates else None
    # We'll fill added_memory_ids after writing
    added_memory_ids: list[int] = []

    # Write memories
    for mem in valid_memories:
        memory_id = _write_memory(
            content=mem["content"],
            memory_type=mem.get("memory_type", "episodic"),
            source=mem.get("source", f"dream:{dream_date}"),
            importance=float(mem.get("importance", 0.8)),
        )
        if memory_id is not None:
            added_memory_ids.append(memory_id)

    # Apply agent.md updates
    agent_md_updated = False
    if agent_md_updates and AGENT_MD_PATH.exists():
        current_md = AGENT_MD_PATH.read_text(encoding="utf-8")
        new_md = current_md
        applied_sections = []
        for update in agent_md_updates[:MAX_AGENT_MD_SECTIONS]:
            section = update.get("section", "").strip()
            new_content = update.get("new_content", "").strip()
            if not section or not new_content:
                continue
            # Look for the section heading in agent.md and replace its content
            # Simple approach: append if section not found, replace if found
            import re
            # Match "## Section" or "# Section" headings
            pattern = re.compile(
                rf"(^#{1,3}\s+{re.escape(section)}\s*\n)(.*?)(?=^#{1,3}\s|\Z)",
                re.MULTILINE | re.DOTALL,
            )
            if pattern.search(new_md):
                new_md = pattern.sub(
                    lambda m: m.group(1) + new_content + "\n\n",
                    new_md,
                )
            else:
                # Append new section at the end
                new_md = new_md.rstrip() + f"\n\n## {section}\n\n{new_content}\n"
            applied_sections.append(section)

        if new_md != current_md:
            AGENT_MD_PATH.write_text(new_md, encoding="utf-8")
            agent_md_updated = True

    # Save snapshot for undo
    _save_snapshot(added_memory_ids, agent_md_before, dream_date=dream_date, turns_hash=current_hash)

    # Append to dream log
    _append_dream_log(dream_date, reflection_summary, len(valid_memories), agent_md_updated)
    habit_review = _habit_store().nightly_review()
    habit_review_text = _habit_store().format_nightly_review(habit_review)
    _append_dream_log(dream_date, f"[habit-review] {habit_review.summary}", 0, False)

    # Process user /good and /bad signals (max 3 per dream to avoid overwhelm)
    signal_log_lines = _habit_store().process_user_signals(
        api_key=backend,
        call_llm_fn=_call_current_backend,
        max_signals=3,
        max_habits_per_signal=2,
        max_context_words=6000,
    )
    if signal_log_lines:
        for sline in signal_log_lines:
            _append_dream_log(dream_date, sline, 0, False)

    recommendation_report = HabitStore.generate_recommendation_report(
        project_root=PROJECT_ROOT,
        generated_by=AGENT_NAME,
        lookback_days=7,
    )
    recommendation_report_summary = HabitStore.summarize_recommendation_report(recommendation_report)
    _append_dream_log(dream_date, f"[habit-report] {recommendation_report_summary}", 0, False)

    # Build output message
    lines = [
        f"🌙 Dream complete — {dream_date}",
        "",
        f"💭 Reflection:",
        reflection_summary,
        "",
    ]
    if forgotten_count > 0:
        lines.append(f"🗑️ Forgot {forgotten_count} old memories (store was {current_memory_count}, trimming to ~{MEMORY_FORGET_TARGET}):")
        for s in forgotten_summaries[:5]:
            lines.append(f"  • {s}")
        if len(forgotten_summaries) > 5:
            lines.append(f"  • ...and {len(forgotten_summaries) - 5} more")
        lines.append("")

    if valid_memories:
        lines.append(f"🧠 {len(valid_memories)} new memories saved:")
        for m in valid_memories:
            imp = float(m.get("importance", 0.8))
            importance_bar = "█" * round(imp * 5)
            # Show more text for high-importance memories
            preview_len = 200 if imp >= 0.9 else 120
            lines.append(f"  [{importance_bar}] {m['content'][:preview_len]}")
        lines.append("")
    else:
        lines.append("🧠 No new memories — nothing significant enough today.")
        lines.append("")

    if agent_md_updated:
        updated_names = [u.get("section", "?") for u in agent_md_updates[:MAX_AGENT_MD_SECTIONS]]
        lines.append(f"📝 Agent.md updated: {', '.join(updated_names)}")
        lines.append("")

    lines.append(habit_review_text)
    lines.append("")
    if signal_log_lines:
        lines.append(f"💬 User signals processed: {len(signal_log_lines)}")
        for sline in signal_log_lines:
            lines.append(f"  • {sline}")
        lines.append("")
    lines.append(f"🧾 {recommendation_report_summary}")
    lines.append(f"📍 Report: {recommendation_report.markdown_path}")
    lines.append("")
    lines.append("Use '/skill dream undo' to revert if needed.")

    return "\n".join(lines)


def cmd_undo() -> str:
    """Revert the most recent dream — pure file restore, no LLM."""
    snapshot = _load_latest_snapshot()
    if not snapshot:
        return "❌ Dream undo: No snapshot found. Nothing to revert."

    snap_date = snapshot.get("date", "unknown")
    reverted_items = []

    # Revert memories
    added_ids = snapshot.get("added_memory_ids", [])
    if added_ids:
        _delete_memories_by_ids(added_ids)
        reverted_items.append(f"Removed {len(added_ids)} memories")

    # Revert agent.md
    agent_md_before = snapshot.get("agent_md_before")
    if agent_md_before is not None:
        AGENT_MD_PATH.write_text(agent_md_before, encoding="utf-8")
        reverted_items.append("Restored agent.md")

    # Remove the snapshot so undo can't be applied twice
    snapshots = sorted(DREAM_DIR.glob("dream_*.json"), reverse=True)
    if snapshots:
        snapshots[0].unlink(missing_ok=True)

    if not reverted_items:
        return f"🌙 Dream undo ({snap_date}): Snapshot existed but nothing to revert (dream may have made no changes)."

    return f"✅ Dream undo complete ({snap_date}):\n" + "\n".join(f"  • {item}" for item in reverted_items)


# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    cmd = (sys.argv[1].lower().strip() if len(sys.argv) > 1 else "status")

    if cmd == "on":
        print(cmd_on())
    elif cmd == "off":
        print(cmd_off())
    elif cmd in ("now", "run"):
        is_scheduled = cmd == "run"
        print(cmd_now(is_scheduled=is_scheduled))
    elif cmd == "undo":
        print(cmd_undo())
    elif cmd == "status":
        print(cmd_status())
    else:
        print(f"Unknown command: {cmd}\nUsage: dream.py [on|off|now|run|undo|status]")
        sys.exit(1)


if __name__ == "__main__":
    main()
