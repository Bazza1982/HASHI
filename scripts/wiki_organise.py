#!/usr/bin/env python3
"""
HASHI Wiki Organiser — data prep utility (no LLM)

Handles:
  1. Update Daily/ pages with specific topic tags  (--tags)
  2. Dump topic/project/weekly memory data to text  (--dump)

LLM generation (topic summaries, project pages, weekly digest) is handled
by Lily agent directly — triggered via HChat from wiki_organise_cron.sh.

Usage:
    python wiki_organise.py              # tags + dump
    python wiki_organise.py --dry-run    # preview without writing
    python wiki_organise.py --tags       # daily tags only
    python wiki_organise.py --dump       # dump memory data only
"""

import sqlite3
import json
import os
import sys
import re
from datetime import datetime, date, timedelta
from collections import defaultdict

# ── Config ─────────────────────────────────────────────────────────────────────
HASHI_ROOT      = "/home/lily/projects/hashi"
CONSOLIDATED_DB = f"{HASHI_ROOT}/workspaces/lily/consolidated_memory.sqlite"
VAULT_ROOT      = "/mnt/c/Users/thene/Documents/lily_hashi_wiki"
DUMP_DIR        = f"{HASHI_ROOT}/workspaces/lily/wiki_dump"

# ── Topic definitions ──────────────────────────────────────────────────────────
TOPICS = {
    "HASHI_Architecture": {
        "display": "HASHI Architecture",
        "desc": "HASHI multi-agent OS framework, agent management, bridge memory, orchestrator",
        "keywords": ["hashi", "agent.md", "bridge", "orchestrator", "openclaw", "hchat",
                     "cron", "scheduler", "hot-restart", "gateway"],
        "max_samples": 60,
    },
    "AI_Memory_Systems": {
        "display": "AI Memory Systems",
        "desc": "Vector memory, SQLite storage, BGE-M3 embedding, memory consolidation",
        "keywords": ["memory", "vector", "embedding", "sqlite", "consolidat", "bgem3", "bge-m3",
                     "bridge_memory", "long-term"],
        "max_samples": 50,
    },
    "Nagare_Workflow": {
        "display": "Nagare Workflow Engine",
        "desc": "HITL workflow engine, Nagare, Shimanto, workflow steps, JobQueue",
        "keywords": ["nagare", "workflow", "hitl", "shimanto", "jobqueue", "job queue",
                     "checkpoint", "workflow engine"],
        "max_samples": 50,
    },
    "Minato_Platform": {
        "display": "Minato Platform",
        "desc": "Minato universal agentic AI OS, plugin-socket architecture, Veritas, KASUMI",
        "keywords": ["minato", "plugin", "socket", "veritas", "kasumi", "aipm", "agentic os"],
        "max_samples": 50,
    },
    "Dream_System": {
        "display": "Dream & Memory Reflection",
        "desc": "Dream nightly reflection, memory promotion, habit tracking",
        "keywords": ["dream", "reflect", "/dream", "habit", "promotion", "nightly"],
        "max_samples": 40,
    },
    "Obsidian_Wiki": {
        "display": "Obsidian Wiki (this wiki)",
        "desc": "Wiki design, Obsidian vault, memory export, backlinks",
        "keywords": ["obsidian", "vault", "wiki", "backlink", "memory_to_obsidian"],
        "max_samples": 30,
    },
    "Carbon_Accounting": {
        "display": "Carbon Accounting Research",
        "desc": "Carbon accounting, GHG protocol, emissions, Zelda's research",
        "keywords": ["carbon", "ghg", "emission", "scope", "accounting", "sustainability"],
        "max_samples": 40,
    },
    "Lily_Remote": {
        "display": "Lily Remote / Hashi Remote",
        "desc": "Remote control app, Lily's first project, Hashi Remote component",
        "keywords": ["lily-remote", "hashi remote", "lily_remote", "remote"],
        "max_samples": 30,
    },
}

# ── Projects ───────────────────────────────────────────────────────────────────
PROJECTS = {
    "HASHI": {
        "display": "HASHI — Multi-Agent OS",
        "desc": "The core HASHI multi-agent framework running HASHI1, HASHI2, HASHI9",
        "keywords": ["hashi", "instance", "agent", "workspace", "hot-restart", "bridge",
                     "orchestrator", "HASHI1", "HASHI2", "HASHI9"],
        "status": "active",
        "max_samples": 60,
    },
    "Minato": {
        "display": "Minato — Agentic AI OS",
        "desc": "Universal agentic AI OS platform assembling Veritas/KASUMI/AIPM/Nagare/HASHI",
        "keywords": ["minato", "plugin-socket", "veritas", "aipm", "workbench"],
        "status": "active",
        "max_samples": 50,
    },
    "Nagare": {
        "display": "Nagare — HITL Workflow Engine",
        "desc": "HITL-native workflow engine, planned as standalone repo",
        "keywords": ["nagare", "hitl", "workflow", "shimanto", "jobqueue"],
        "status": "active",
        "max_samples": 40,
    },
    "Florence2": {
        "display": "Florence-2 Vision Pipeline",
        "desc": "Image preprocessing pipeline to reduce token consumption using Florence-2",
        "keywords": ["florence", "florence-2", "vision", "image preprocessing", "token consumption"],
        "status": "planned",
        "max_samples": 20,
    },
}

# ── Helpers ────────────────────────────────────────────────────────────────────
def truncate(text: str, n: int = 200) -> str:
    text = text.replace("\n", " ").strip()
    return text[:n] + "…" if len(text) > n else text

def write_file(path: str, content: str, dry_run: bool = False):
    if dry_run:
        print(f"  [DRY] {path}")
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def fmt_date(ts: str) -> str:
    return ts[:10] if ts and len(ts) >= 10 else (ts or "?")

# ── Database queries ───────────────────────────────────────────────────────────
def get_topic_memories(conn: sqlite3.Connection, keywords: list,
                       max_samples: int = 50) -> list:
    conditions = " OR ".join(f"lower(content) LIKE ?" for _ in keywords)
    params = [f"%{kw.lower()}%" for kw in keywords]
    rows = conn.execute(f"""
        SELECT agent_id, instance, domain, memory_type, content, source_ts, ts_source
        FROM consolidated
        WHERE ({conditions})
        ORDER BY source_ts DESC
        LIMIT {max_samples}
    """, params).fetchall()
    return [
        {"agent": r[0], "instance": r[1], "domain": r[2], "memory_type": r[3],
         "content": r[4], "date": fmt_date(r[5]), "source": r[6]}
        for r in rows
    ]

def get_week_memories(conn: sqlite3.Connection, week_start: date, week_end: date) -> list:
    rows = conn.execute("""
        SELECT agent_id, instance, domain, memory_type, content, source_ts, ts_source
        FROM consolidated
        WHERE source_ts >= ? AND source_ts < ?
        ORDER BY source_ts ASC
    """, (week_start.isoformat(), week_end.isoformat())).fetchall()
    return [
        {"agent": r[0], "instance": r[1], "domain": r[2], "memory_type": r[3],
         "content": r[4], "date": fmt_date(r[5]), "source": r[6]}
        for r in rows
    ]

# ── Tag updater ────────────────────────────────────────────────────────────────
TOPIC_KEYWORD_TAGS = {
    "hashi": "hashi-architecture",
    "nagare": "nagare",
    "minato": "minato",
    "workflow": "nagare",
    "memory": "ai-memory",
    "embedding": "ai-memory",
    "vector": "ai-memory",
    "dream": "dream-system",
    "carbon": "carbon-accounting",
    "ghg": "carbon-accounting",
    "obsidian": "obsidian-wiki",
    "wiki": "obsidian-wiki",
    "nathan": "education-nathan",
    "alex": "education-alex",
    "email": "email-management",
    "lily-remote": "lily-remote",
}

def get_content_tags(content: str) -> list:
    lower = content.lower()
    tags = set()
    for kw, tag in TOPIC_KEYWORD_TAGS.items():
        if kw in lower:
            tags.add(tag)
    return sorted(tags)

def update_daily_tags(conn: sqlite3.Connection, dry_run: bool) -> int:
    rows = conn.execute("""
        SELECT date(source_ts), GROUP_CONCAT(content, '|||'), COUNT(*)
        FROM consolidated
        GROUP BY date(source_ts)
        ORDER BY date(source_ts)
    """).fetchall()

    updated = 0
    for row in rows:
        day_date = row[0]
        all_content = row[1] or ""
        path = f"{VAULT_ROOT}/Daily/{day_date}.md"
        if not os.path.exists(path):
            continue

        specific_tags = get_content_tags(all_content[:5000])
        base_tags = ["daily"]
        all_tags = base_tags + specific_tags

        with open(path, encoding="utf-8") as f:
            content = f.read()

        new_tags_line = f"tags: [{', '.join(all_tags)}]"
        new_content = re.sub(r"^tags: \[.*?\]$", new_tags_line,
                             content, flags=re.MULTILINE)
        if new_content != content:
            write_file(path, new_content, dry_run)
            updated += 1

    return updated

# ── Memory dump (for Lily to use) ──────────────────────────────────────────────
def dump_memories(conn: sqlite3.Connection, dry_run: bool) -> dict:
    """Dump memory data for each topic/project/week to files in DUMP_DIR.
    Returns a summary dict for the cron to report."""
    if dry_run:
        print("  [DRY] Would dump memory data to", DUMP_DIR)
        return {}

    os.makedirs(DUMP_DIR, exist_ok=True)
    summary = {"topics": {}, "projects": {}, "weekly": None}

    # Topics
    for topic_id, meta in TOPICS.items():
        memories = get_topic_memories(conn, meta["keywords"], meta["max_samples"])
        path = f"{DUMP_DIR}/topic_{topic_id}.json"
        data = {"topic_id": topic_id, "meta": meta, "memories": memories}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        summary["topics"][topic_id] = len(memories)
        print(f"  Dumped {len(memories)} memories → topic_{topic_id}.json")

    # Projects
    for proj_id, meta in PROJECTS.items():
        memories = get_topic_memories(conn, meta["keywords"], meta["max_samples"])
        path = f"{DUMP_DIR}/project_{proj_id}.json"
        data = {"proj_id": proj_id, "meta": meta, "memories": memories}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        summary["projects"][proj_id] = len(memories)
        print(f"  Dumped {len(memories)} memories → project_{proj_id}.json")

    # Weekly
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    week_end   = week_start + timedelta(days=7)
    week_str   = week_start.strftime("%Y-W%V")
    memories   = get_week_memories(conn, week_start, week_end)
    path = f"{DUMP_DIR}/weekly_{week_str}.json"
    agents_seen = sorted(set(m["agent"] for m in memories))
    data = {
        "week_str": week_str,
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "agents": agents_seen,
        "memories": memories,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    summary["weekly"] = {"week": week_str, "count": len(memories), "path": path}
    print(f"  Dumped {len(memories)} memories → weekly_{week_str}.json")

    # Write a manifest so Lily knows what to process
    manifest = {
        "generated": date.today().isoformat(),
        "dump_dir": DUMP_DIR,
        "vault_root": VAULT_ROOT,
        "topics": list(TOPICS.keys()),
        "projects": list(PROJECTS.keys()),
        "weekly": summary["weekly"],
        "summary": summary,
    }
    with open(f"{DUMP_DIR}/manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    return summary

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    dry_run  = "--dry-run" in sys.argv
    do_tags  = "--tags"    in sys.argv or len(sys.argv) == 1 or sys.argv[1:] == ["--dry-run"]
    do_dump  = "--dump"    in sys.argv or len(sys.argv) == 1 or sys.argv[1:] == ["--dry-run"]

    print("=" * 65)
    print("HASHI Wiki Organiser (data prep — no LLM)")
    print(f"  Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"  Tasks: tags={do_tags} dump={do_dump}")
    print("=" * 65)

    conn = sqlite3.connect(CONSOLIDATED_DB)
    os.makedirs(f"{VAULT_ROOT}/Weekly", exist_ok=True)

    # ── 1. Daily tags ──────────────────────────────────────────────────────────
    if do_tags:
        print(f"\n{'─'*65}")
        print("PHASE 1: Updating Daily/ tags")
        print(f"{'─'*65}")
        updated = update_daily_tags(conn, dry_run)
        print(f"  Updated {updated} daily pages with specific tags")

    # ── 2. Dump memory data for Lily ───────────────────────────────────────────
    if do_dump:
        print(f"\n{'─'*65}")
        print("PHASE 2: Dumping memory data for Lily")
        print(f"{'─'*65}")
        summary = dump_memories(conn, dry_run)

    conn.close()

    print(f"\n{'='*65}")
    print("Wiki data prep complete.")
    print(f"  Topics    : {len(TOPICS)}")
    print(f"  Projects  : {len(PROJECTS)}")
    print(f"  Dump dir  : {DUMP_DIR}")
    print(f"  Vault     : {VAULT_ROOT}")
    if dry_run:
        print("  [DRY RUN — no files written]")

if __name__ == "__main__":
    main()
