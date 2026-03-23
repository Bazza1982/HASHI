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

import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ─── paths ───────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(os.environ.get("BRIDGE_PROJECT_ROOT", Path(__file__).parent.parent.parent))
WORKSPACE_DIR = Path(os.environ.get("BRIDGE_WORKSPACE_DIR", PROJECT_ROOT / "workspaces" / "lily"))
AGENT_NAME = WORKSPACE_DIR.name
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from orchestrator.bridge_memory import BridgeMemoryStore

TASKS_PATH = PROJECT_ROOT / "tasks.json"
SECRETS_PATH = PROJECT_ROOT / "secrets.json"
TRANSCRIPT_PATH = WORKSPACE_DIR / "transcript.jsonl"
AGENT_MD_PATH = WORKSPACE_DIR / "AGENT.md"
DREAM_DIR = WORKSPACE_DIR / "dream_snapshots"
DREAM_LOG_PATH = WORKSPACE_DIR / "dream_log.md"
MEMORY_DB_PATH = WORKSPACE_DIR / "bridge_memory.sqlite"

CRON_JOB_ID = f"dream-{AGENT_NAME}-nightly"
CRON_TIME = "01:30"

MAX_NEW_MEMORIES = 5
MIN_IMPORTANCE = 0.7
MAX_AGENT_MD_SECTIONS = 3
SNAPSHOT_RETENTION_DAYS = 7
MEMORY_FORGET_THRESHOLD = 200   # start forgetting when agent exceeds this many memories
MEMORY_FORGET_TARGET = 160      # trim down to this count when threshold exceeded
MAX_FORGET_PER_DREAM = 20       # safety cap — never delete more than this in one run
_MEMORY_STORE: BridgeMemoryStore | None = None


# ─── helpers ─────────────────────────────────────────────────────────────────

def _read_secrets() -> dict:
    try:
        return json.loads(SECRETS_PATH.read_text())
    except Exception:
        return {}


def _get_openrouter_key(secrets: dict) -> str | None:
    for key in (f"{AGENT_NAME}_openrouter_key", "openrouter-api_key", "openrouter_key"):
        if secrets.get(key):
            return secrets[key]
    return None


def _read_tasks() -> dict:
    try:
        return json.loads(TASKS_PATH.read_text())
    except Exception:
        return {"version": 1, "heartbeats": [], "crons": []}


def _write_tasks(tasks: dict):
    TASKS_PATH.write_text(json.dumps(tasks, indent=2, ensure_ascii=False))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _load_today_transcript() -> list[dict]:
    """Load today's user+assistant turns from transcript.jsonl."""
    if not TRANSCRIPT_PATH.exists():
        return []
    today = _today_str()
    turns = []
    try:
        for line in TRANSCRIPT_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("role") in ("user", "assistant"):
                turns.append(entry)
    except Exception:
        pass
    return turns


def _call_openrouter(api_key: str, messages: list[dict], model: str = "anthropic/claude-sonnet-4-5") -> str:
    """Call OpenRouter chat completions API. Returns text response."""
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": 2000,
        "temperature": 0.3,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/hashi-bridge",
            "X-Title": "Hashi Dream",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenRouter HTTP {e.code}: {body[:300]}") from e


def _memory_store() -> BridgeMemoryStore:
    global _MEMORY_STORE
    if _MEMORY_STORE is None:
        _MEMORY_STORE = BridgeMemoryStore(WORKSPACE_DIR)
    return _MEMORY_STORE


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


def _forget_memories(api_key: str, current_count: int) -> tuple[int, list[str]]:
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
        response = _call_openrouter(api_key, [{"role": "user", "content": forget_prompt}])
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

def _save_snapshot(added_memory_ids: list[int], agent_md_before: str | None) -> Path:
    DREAM_DIR.mkdir(exist_ok=True)
    today = _today_str()
    snapshot = {
        "date": today,
        "ts": _now_iso(),
        "agent_md_before": agent_md_before,
        "added_memory_ids": added_memory_ids,
    }
    path = DREAM_DIR / f"dream_{today}.json"
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
    secrets = _read_secrets()
    api_key = _get_openrouter_key(secrets)
    if not api_key:
        return "❌ Dream: No OpenRouter API key found. Cannot run reflection."

    turns = _load_today_transcript()
    if len(turns) < 4:
        return (
            "🌙 Dream skipped — not enough conversation today to reflect on "
            f"(found {len(turns)} turns, need at least 4)."
        )

    # Build transcript excerpt (cap at ~6000 chars to stay within context)
    transcript_text = ""
    for t in turns[-60:]:
        role = "User" if t["role"] == "user" else "Assistant"
        transcript_text += f"{role}: {t['text'][:800]}\n\n"
    if len(transcript_text) > 6000:
        transcript_text = transcript_text[-6000:]

    agent_md_content = AGENT_MD_PATH.read_text(encoding="utf-8") if AGENT_MD_PATH.exists() else ""

    # Build dream prompt
    dream_prompt = f"""You are performing a nightly memory consolidation for an AI assistant agent named {AGENT_NAME}.

Review today's conversation transcript and decide what is truly worth remembering long-term to serve the user better.

Think like a human brain during sleep: extract the essence, compress the important, discard the noise.

TODAY'S TRANSCRIPT:
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
      "content": "Concise memory text — should be self-contained and useful in future conversations",
      "memory_type": "episodic",
      "importance": 0.8,
      "source": "dream:{_today_str()}"
    }}
  ],
  "agent_md_updates": [
    {{
      "section": "Section name or heading to update",
      "reason": "Why this update matters",
      "new_content": "The exact new content for this section"
    }}
  ],
  "reflection_summary": "2-4 sentence summary of what was learned today and why it matters for future interactions. Write in first person as the agent."
}}

RULES (follow strictly):
1. new_memories: max {MAX_NEW_MEMORIES} entries, only if importance >= {MIN_IMPORTANCE}
2. agent_md_updates: max {MAX_AGENT_MD_SECTIONS} sections, only for genuinely significant insights
3. Do NOT invent facts — only extract from the actual transcript
4. Do NOT update agent.md for trivial things; only for meaningful behavioral insights
5. If nothing significant happened today, return empty arrays and say so in reflection_summary
6. new_memories array may be empty, agent_md_updates array may be empty
"""

    try:
        response = _call_openrouter(api_key, [{"role": "user", "content": dream_prompt}])
    except RuntimeError as e:
        return f"❌ Dream: LLM call failed — {e}"

    # Parse JSON response
    try:
        # Strip markdown code blocks if present
        clean = response.strip()
        if clean.startswith("```"):
            clean = "\n".join(clean.split("\n")[1:])
            if clean.endswith("```"):
                clean = clean[:-3]
        dream_result = json.loads(clean.strip())
    except json.JSONDecodeError as e:
        return f"❌ Dream: Failed to parse LLM response as JSON — {e}\n\nRaw response:\n{response[:500]}"

    new_memories: list[dict] = dream_result.get("new_memories", [])
    agent_md_updates: list[dict] = dream_result.get("agent_md_updates", [])
    reflection_summary: str = dream_result.get("reflection_summary", "No summary provided.")

    # Filter memories by importance threshold and cap count
    valid_memories = [
        m for m in new_memories
        if isinstance(m, dict) and float(m.get("importance", 0)) >= MIN_IMPORTANCE
    ][:MAX_NEW_MEMORIES]

    # ── Forgetting phase (before adding new memories) ──────────────────────────
    current_memory_count = _count_memories()
    forgotten_count = 0
    forgotten_summaries: list[str] = []
    if current_memory_count > MEMORY_FORGET_THRESHOLD:
        forgotten_count, forgotten_summaries = _forget_memories(api_key, current_memory_count)

    # Save snapshot BEFORE applying changes
    agent_md_before = agent_md_content if agent_md_updates else None
    # We'll fill added_memory_ids after writing
    added_memory_ids: list[int] = []

    # Write memories
    for mem in valid_memories:
        memory_id = _write_memory(
            content=mem["content"],
            memory_type=mem.get("memory_type", "episodic"),
            source=mem.get("source", f"dream:{_today_str()}"),
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
    _save_snapshot(added_memory_ids, agent_md_before)

    # Append to dream log
    _append_dream_log(_today_str(), reflection_summary, len(valid_memories), agent_md_updated)

    # Build output message
    lines = [
        f"🌙 Dream complete — {_today_str()}",
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
            importance_bar = "█" * round(float(m.get("importance", 0.8)) * 5)
            lines.append(f"  [{importance_bar}] {m['content'][:120]}")
        lines.append("")
    else:
        lines.append("🧠 No new memories — nothing significant enough today.")
        lines.append("")

    if agent_md_updated:
        updated_names = [u.get("section", "?") for u in agent_md_updates[:MAX_AGENT_MD_SECTIONS]]
        lines.append(f"📝 Agent.md updated: {', '.join(updated_names)}")
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
