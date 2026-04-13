#!/usr/bin/env python3
"""
HASHI Wiki Organiser — LLM-powered weekly wiki curation

Transforms the raw memory dump in lily_hashi_wiki into a real knowledge wiki:
1. Upgrades Topics/ pages with curated summaries + key insights
2. Populates Projects/ pages from memories
3. Generates Weekly/ digest grouped by theme
4. Updates all Daily/ pages with specific topic tags

Usage:
    python wiki_organise.py              # full run
    python wiki_organise.py --dry-run    # preview without writing
    python wiki_organise.py --topics     # topics only
    python wiki_organise.py --projects   # projects only
    python wiki_organise.py --weekly     # weekly digest only
"""

import sqlite3
import json
import os
import sys
import re
from datetime import datetime, date, timedelta
from collections import defaultdict
import urllib.request

# ── Config ─────────────────────────────────────────────────────────────────────
HASHI_ROOT      = "/home/lily/projects/hashi"
CONSOLIDATED_DB = f"{HASHI_ROOT}/workspaces/lily/consolidated_memory.sqlite"
VAULT_ROOT      = "/mnt/c/Users/thene/Documents/lily_hashi_wiki"
SECRETS_FILE    = f"{HASHI_ROOT}/secrets.json"

# OpenRouter config
OPENROUTER_URL   = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "anthropic/claude-sonnet-4-5"
MAX_TOKENS       = 2000

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
def load_api_key() -> str:
    secrets = json.load(open(SECRETS_FILE))
    key = secrets.get("openrouter_key", "")
    if not key:
        raise ValueError("openrouter_key not found in secrets.json")
    return key

def call_llm(api_key: str, system: str, user: str) -> str:
    payload = json.dumps({
        "model": OPENROUTER_MODEL,
        "max_tokens": MAX_TOKENS,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
    }).encode("utf-8")
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://hashi.local",
            "X-Title": "HASHI Wiki Organiser",
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    return result["choices"][0]["message"]["content"].strip()

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
def get_topic_memories(conn: sqlite3.Connection, keywords: list[str],
                       max_samples: int = 50) -> list[dict]:
    """Get memories matching any of the given keywords, ordered by recency."""
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

def get_week_memories(conn: sqlite3.Connection, week_start: date, week_end: date) -> list[dict]:
    """Get all memories from a given week."""
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

# ── Topics organiser ───────────────────────────────────────────────────────────
TOPIC_SYSTEM = """You are a knowledge curator for a private AI agent team wiki.
Your job: read a set of raw agent memories about a specific topic and produce a well-structured wiki page section.

Rules:
- Be factual: only state things that appear in the memories
- Be concise but comprehensive
- Write in a mix of English and Chinese (match the source material's language)
- Format output as clean Markdown sections (no outer code fences)
- Do not invent details not in the memories"""

def summarise_topic(api_key: str, topic_id: str, meta: dict,
                    memories: list[dict]) -> str:
    """Call LLM to generate a curated topic page."""
    # Prepare memory samples for the prompt
    samples = []
    for m in memories[:40]:  # cap at 40 for prompt size
        samples.append(f"[{m['date']}] [{m['agent']}] [{m['domain']}] {truncate(m['content'], 180)}")
    mem_text = "\n".join(samples)

    prompt = f"""Topic: **{meta['display']}**
Description: {meta['desc']}

Below are {len(memories)} raw agent memories related to this topic (most recent first):

{mem_text}

---

Please generate a wiki page for this topic with these EXACT sections:

## 主题概述
(2-3 sentences: what this topic is about, why it matters to the team)

## 核心知识点
(5-10 bullet points: the most important facts, decisions, or principles discovered about this topic)

## 关键决策记录
(table with columns: 日期 | 决策内容 | 决策者/来源 — list max 8 most important decisions)

## 最近动态
(3-5 bullets: what happened most recently with this topic, with dates)

## 相关 Agents
(list agents who worked on this topic with a one-line description of their role)"""

    return call_llm(api_key, TOPIC_SYSTEM, prompt)

def build_topic_page(topic_id: str, meta: dict, llm_content: str,
                     memories: list[dict], now: str) -> str:
    # Collect all agents and dates involved
    agents = sorted(set(m["agent"] for m in memories))
    date_range_start = min(m["date"] for m in memories) if memories else "?"
    date_range_end = max(m["date"] for m in memories) if memories else "?"

    # Build tag list
    tags = ["topic", topic_id.lower().replace("_", "-")]

    lines = [
        f"---",
        f"type: topic",
        f"name: {topic_id}",
        f"display: {meta['display']}",
        f"memory_count: {len(memories)}",
        f"date_range: {date_range_start} → {date_range_end}",
        f"last_updated: {now}",
        f"tags: [{', '.join(tags)}]",
        f"---",
        f"",
        f"# {meta['display']}",
        f"",
        f"> **范围:** {meta['desc']}  ",
        f"> **记忆条数:** {len(memories)} · **日期跨度:** {date_range_start} → {date_range_end}  ",
        f"> **涉及 agents:** {', '.join(f'[[Agents/{a}]]' for a in agents)}",
        f"",
        f"---",
        f"",
        llm_content,
        f"",
        f"---",
        f"",
        f"## 原始记忆条目（最近20条）",
        f"",
        f"| 日期 | Agent | 内容摘要 |",
        f"|------|-------|---------|",
    ]
    for m in memories[:20]:
        agent_link = f"[[Agents/{m['agent']}]]"
        lines.append(f"| {m['date']} | {agent_link} | {truncate(m['content'], 100)} |")

    lines += ["", f"_自动生成 · {now}_"]
    return "\n".join(lines)

# ── Projects organiser ─────────────────────────────────────────────────────────
PROJECT_SYSTEM = """You are a technical project manager writing a wiki page for an AI development project.
Read the agent memories and produce a clear, structured project overview page.
Be factual, precise, and helpful. Mix English/Chinese as the source material does."""

def summarise_project(api_key: str, proj_id: str, meta: dict,
                      memories: list[dict]) -> str:
    samples = []
    for m in memories[:40]:
        samples.append(f"[{m['date']}] [{m['agent']}] {truncate(m['content'], 180)}")
    mem_text = "\n".join(samples)

    prompt = f"""Project: **{meta['display']}**
Description: {meta['desc']}
Status: {meta['status']}

Agent memories about this project ({len(memories)} total, sample shown):

{mem_text}

---

Generate a wiki project page with these EXACT sections:

## 项目概述
(3-4 sentences about what this project is, its purpose, and current state)

## 技术架构
(bullet points describing key technical components, design decisions, and architecture)

## 里程碑 & 进展
(timeline of key milestones, ordered chronologically, with dates where known)

## 当前状态
(what is working, what is in progress, what is blocked or pending)

## 参与 Agents
(which agents worked on this, and what they each contributed)

## 下一步
(known next steps or open questions)"""

    return call_llm(api_key, PROJECT_SYSTEM, prompt)

def build_project_page(proj_id: str, meta: dict, llm_content: str,
                       memories: list[dict], now: str) -> str:
    agents = sorted(set(m["agent"] for m in memories))
    status_emoji = {"active": "🟢", "planned": "🟡", "archived": "⚫"}.get(meta["status"], "⚪")

    lines = [
        f"---",
        f"type: project",
        f"name: {proj_id}",
        f"status: {meta['status']}",
        f"last_updated: {now}",
        f"tags: [project, {meta['status']}]",
        f"---",
        f"",
        f"# {meta['display']}",
        f"",
        f"**状态:** {status_emoji} {meta['status'].title()} · **最后更新:** {now}",
        f"",
        f"---",
        f"",
        llm_content,
        f"",
        f"---",
        f"",
        f"_自动生成 · {now}_",
    ]
    return "\n".join(lines)

# ── Weekly digest ──────────────────────────────────────────────────────────────
WEEKLY_SYSTEM = """You are writing a weekly digest for a team of AI agents working together.
Read the week's memories and produce an insightful, well-organised summary.
Group by theme, highlight key decisions, and write as if briefing a manager.
Mix English/Chinese as the source material does."""

def build_weekly_digest(api_key: str, week_start: date, week_end: date,
                        memories: list[dict], dry_run: bool):
    week_str = f"{week_start.strftime('%Y-W%V')}"
    now = date.today().isoformat()
    path = f"{VAULT_ROOT}/Weekly/{week_str}.md"

    print(f"\n[Weekly] {week_str} ({len(memories)} memories, {week_start} → {week_end})")

    if not memories:
        print("  No memories this week, skipping.")
        return

    # Sample memories for prompt
    samples = []
    agents_seen = set()
    for m in memories:
        agents_seen.add(m["agent"])
        samples.append(f"[{m['date']}] [{m['agent']}] [{m['domain']}] {truncate(m['content'], 160)}")

    # Cap at 80 for prompt size
    if len(samples) > 80:
        # Take every Nth to get a representative sample
        step = len(samples) // 80
        samples = samples[::step][:80]

    mem_text = "\n".join(samples)

    prompt = f"""Week: {week_str} ({week_start} to {week_end})
Active agents: {', '.join(sorted(agents_seen))}
Total memories: {len(memories)} (sample of {len(samples)} shown)

Memory sample:
{mem_text}

---

Write a weekly digest with these EXACT sections:

## 本周摘要
(3-4 sentence executive summary of what happened this week)

## 主要主题
(3-6 thematic groups — give each theme a name, then 3-5 bullet points of what happened)

## 重要决策
(bullet list of significant decisions made this week, with agent and date where known)

## 本周亮点
(2-3 notable achievements or breakthroughs)

## 遗留问题 & 下周关注
(open issues, blockers, or things that need attention next week)

## Agent 活跃度
(brief note on which agents were most active and what they focused on)"""

    print("  Calling LLM for weekly summary...")
    if dry_run:
        print(f"  [DRY] Would write {path}")
        return

    llm_content = call_llm(api_key, WEEKLY_SYSTEM, prompt)

    agent_links = " · ".join(f"[[Agents/{a}]]" for a in sorted(agents_seen))
    page = "\n".join([
        f"---",
        f"type: weekly",
        f"week: {week_str}",
        f"start: {week_start.isoformat()}",
        f"end: {week_end.isoformat()}",
        f"memory_count: {len(memories)}",
        f"agents: [{', '.join(sorted(agents_seen))}]",
        f"last_updated: {now}",
        f"tags: [weekly, digest]",
        f"---",
        f"",
        f"# 每周消化 — {week_str}",
        f"",
        f"**{week_start.strftime('%Y年%m月%d日')} — {week_end.strftime('%m月%d日')}** · {len(memories)} 条记忆",
        f"",
        f"**Active Agents:** {agent_links}",
        f"",
        f"---",
        f"",
        llm_content,
        f"",
        f"---",
        f"_自动生成 · {now}_",
    ])

    write_file(path, page, dry_run)
    print(f"  ✓ Written: {path}")

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

def get_content_tags(content: str) -> list[str]:
    lower = content.lower()
    tags = set()
    for kw, tag in TOPIC_KEYWORD_TAGS.items():
        if kw in lower:
            tags.add(tag)
    return sorted(tags)

def update_daily_tags(conn: sqlite3.Connection, dry_run: bool) -> int:
    """Re-write Daily/ pages with specific topic tags in frontmatter."""
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

        # Get specific tags for this day
        specific_tags = get_content_tags(all_content[:5000])  # sample first 5000 chars
        base_tags = ["daily"]
        all_tags = base_tags + specific_tags

        with open(path, encoding="utf-8") as f:
            content = f.read()

        # Replace tags line in frontmatter
        new_tags_line = f"tags: [{', '.join(all_tags)}]"
        new_content = re.sub(r"^tags: \[.*?\]$", new_tags_line,
                             content, flags=re.MULTILINE)
        if new_content != content:
            write_file(path, new_content, dry_run)
            updated += 1

    return updated

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    dry_run  = "--dry-run" in sys.argv
    do_topics   = "--topics"   in sys.argv or len(sys.argv) == 1 or sys.argv[1:] == ["--dry-run"]
    do_projects = "--projects" in sys.argv or len(sys.argv) == 1 or sys.argv[1:] == ["--dry-run"]
    do_weekly   = "--weekly"   in sys.argv or len(sys.argv) == 1 or sys.argv[1:] == ["--dry-run"]
    do_tags     = "--tags"     in sys.argv or len(sys.argv) == 1 or sys.argv[1:] == ["--dry-run"]

    print("=" * 65)
    print("HASHI Wiki Organiser")
    print(f"  Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"  Tasks: topics={do_topics} projects={do_projects} weekly={do_weekly} tags={do_tags}")
    print("=" * 65)

    api_key = load_api_key()
    conn = sqlite3.connect(CONSOLIDATED_DB)
    now = date.today().isoformat()

    os.makedirs(f"{VAULT_ROOT}/Weekly", exist_ok=True)

    # ── 1. Topics ──────────────────────────────────────────────────────────────
    if do_topics:
        print(f"\n{'─'*65}")
        print("PHASE 1: Topic Pages")
        print(f"{'─'*65}")
        for topic_id, meta in TOPICS.items():
            print(f"\n  [{topic_id}] Fetching memories...")
            memories = get_topic_memories(conn, meta["keywords"], meta["max_samples"])
            print(f"  Found {len(memories)} memories")
            if not memories:
                print("  Skipping (no memories)")
                continue

            if not dry_run:
                print(f"  Calling LLM...")
                llm_content = summarise_topic(api_key, topic_id, meta, memories)
                page = build_topic_page(topic_id, meta, llm_content, memories, now)
                path = f"{VAULT_ROOT}/Topics/{topic_id}.md"
                write_file(path, page, dry_run)
                print(f"  ✓ Written: Topics/{topic_id}.md")
            else:
                print(f"  [DRY] Would write Topics/{topic_id}.md")

    # ── 2. Projects ────────────────────────────────────────────────────────────
    if do_projects:
        print(f"\n{'─'*65}")
        print("PHASE 2: Project Pages")
        print(f"{'─'*65}")
        for proj_id, meta in PROJECTS.items():
            print(f"\n  [{proj_id}] Fetching memories...")
            memories = get_topic_memories(conn, meta["keywords"], meta["max_samples"])
            print(f"  Found {len(memories)} memories")
            if not memories:
                print("  Skipping (no memories)")
                continue

            if not dry_run:
                print(f"  Calling LLM...")
                llm_content = summarise_project(api_key, proj_id, meta, memories)
                page = build_project_page(proj_id, meta, llm_content, memories, now)
                path = f"{VAULT_ROOT}/Projects/{proj_id}.md"
                write_file(path, page, dry_run)
                print(f"  ✓ Written: Projects/{proj_id}.md")
            else:
                print(f"  [DRY] Would write Projects/{proj_id}.md")

    # ── 3. Weekly digest ───────────────────────────────────────────────────────
    if do_weekly:
        print(f"\n{'─'*65}")
        print("PHASE 3: Weekly Digest")
        print(f"{'─'*65}")
        today = date.today()
        # Current week (Mon–Sun)
        week_start = today - timedelta(days=today.weekday())
        week_end   = week_start + timedelta(days=7)
        # Also do previous week if today is Monday
        weeks_to_process = [(week_start, week_end)]
        if today.weekday() == 0:  # Monday — also do last week
            prev_start = week_start - timedelta(days=7)
            prev_end   = week_start
            weeks_to_process.insert(0, (prev_start, prev_end))

        for ws, we in weeks_to_process:
            memories = get_week_memories(conn, ws, we)
            build_weekly_digest(api_key, ws, we, memories, dry_run)

    # ── 4. Daily tags ──────────────────────────────────────────────────────────
    if do_tags:
        print(f"\n{'─'*65}")
        print("PHASE 4: Updating Daily/ tags")
        print(f"{'─'*65}")
        updated = update_daily_tags(conn, dry_run)
        print(f"  Updated {updated} daily pages with specific tags")

    conn.close()

    # ── Summary ────────────────────────────────────────────────────────────────
    print(f"\n{'='*65}")
    print("Wiki Organise complete.")
    print(f"  Topics    : {len(TOPICS)}")
    print(f"  Projects  : {len(PROJECTS)}")
    print(f"  Vault     : {VAULT_ROOT}")
    if dry_run:
        print("  [DRY RUN — no files written]")

if __name__ == "__main__":
    main()
