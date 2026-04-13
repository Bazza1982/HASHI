#!/usr/bin/env python3
"""
HASHI → Obsidian Wiki Exporter

Reads consolidated_memory.sqlite and writes structured Markdown files
to the lily_hashi_wiki Obsidian vault.

Usage:
    python memory_to_obsidian.py               # incremental (new memories only)
    python memory_to_obsidian.py --full         # full re-export of everything
    python memory_to_obsidian.py --dry-run      # print what would be written, no files

Design: one-way (DB → Obsidian). DB is source of truth.
"""

import sqlite3
import json
import os
import sys
import re
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

# ── Paths ──────────────────────────────────────────────────────────────────────
HASHI_ROOT       = "/home/lily/projects/hashi"
CONSOLIDATED_DB  = f"{HASHI_ROOT}/workspaces/lily/consolidated_memory.sqlite"
VAULT_ROOT       = "/mnt/c/Users/thene/Documents/lily_hashi_wiki"
LAST_SYNC_FILE   = f"{VAULT_ROOT}/_meta/last_sync.json"
EMAIL_REPORTS_DIR = f"{HASHI_ROOT}/workspaces/sunny/email_reports"
VAULT_EMAIL_DIR   = f"{VAULT_ROOT}/Dad/EmailReports"

# ── Agent registry ─────────────────────────────────────────────────────────────
AGENTS = {
    "HASHI1": [
        ("lily",     "小蕾"),
        ("akane",    "茜"),
        ("arale",    "阿拉蕾"),
        ("zelda",    "Zelda"),
        ("ying",     "小颖"),
        ("renee",    "小英"),
        ("sunny",    "小夏"),
        ("rain",     "Rain"),
        ("sakura",   "小樱"),
        ("baymax",   "Baymax"),
        ("doraemon", "哆啦A梦"),
    ],
    "HASHI2": [
        ("kasumi",    "霞"),
        ("ajiao",     "阿娇"),
        ("lin_yueru", "林月如"),
        ("rika",      "里香"),
        ("samantha",  "Samantha"),
        ("zhao_ling", "赵灵"),
    ],
    "HASHI9": [
        ("hashiko", "はしこ"),
    ],
}

# Flat map: agent_id -> (display_name, instance)
AGENT_MAP = {}
for inst, agents in AGENTS.items():
    for aid, dname in agents:
        AGENT_MAP[aid] = (dname, inst)

# ── Topic keyword rules ────────────────────────────────────────────────────────
TOPIC_RULES = [
    ("Obsidian_Wiki",      ["obsidian", "vault", "wiki", "backlink"]),
    ("AI_Memory_Systems",  ["memory", "vector", "embedding", "sqlite", "consolidat", "bgem3", "bge-m3"]),
    ("Nagare_Workflow",    ["nagare", "workflow", "hitl", "shimanto"]),
    ("Minato_Platform",    ["minato", "plugin", "socket", "veritas", "kasumi", "aipm"]),
    ("Dream_System",       ["dream", "reflect", "/dream"]),
    ("HASHI_Architecture", ["hashi", "agent.md", "bridge", "openclaw", "hchat", "cron", "scheduler"]),
    ("Carbon_Accounting",  ["carbon", "ghg", "emission", "scope 1", "scope 2", "scope 3", "accounting"]),
    ("Lily_Remote",        ["lily-remote", "hashi remote", "lily_remote"]),
]

# ── Source → Backlink mapping ──────────────────────────────────────────────────
def source_to_backlink(source: str) -> str | None:
    """Convert a memory source string to an Obsidian backlink, or None."""
    if source.startswith("dream:"):
        date = source[6:]
        return f"[[Dreams/{date}]]"
    if source.startswith("hchat:"):
        agent = source[6:]
        if agent in AGENT_MAP:
            return f"[[Agents/{agent}]]"
    if source.startswith("hchat-reply:"):
        agent = source[12:]
        if agent in AGENT_MAP:
            return f"[[Agents/{agent}]]"
    if source.startswith("cos-query:"):
        agent = source[10:]
        if agent in AGENT_MAP:
            return f"[[Agents/{agent}]]"
    return None

# ── Topic detection ────────────────────────────────────────────────────────────
def detect_topics(content: str) -> list[str]:
    """Return list of topic page names that match this memory's content."""
    lower = content.lower()
    matched = []
    for topic_name, keywords in TOPIC_RULES:
        if any(kw in lower for kw in keywords):
            matched.append(topic_name)
    return matched

# ── Helpers ────────────────────────────────────────────────────────────────────
def fmt_date(ts: str) -> str:
    """Extract YYYY-MM-DD from an ISO timestamp string."""
    if ts and len(ts) >= 10:
        return ts[:10]
    return ts or "unknown"

def truncate(text: str, n: int = 100) -> str:
    text = text.replace("\n", " ").strip()
    return text[:n] + "…" if len(text) > n else text

def write_file(path: str, content: str, dry_run: bool = False):
    if dry_run:
        print(f"[DRY] Would write: {path}")
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def append_to_file(path: str, content: str, dry_run: bool = False):
    """Append to file, creating if missing."""
    if dry_run:
        print(f"[DRY] Would append to: {path}")
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(content)

# ── Load sync state ────────────────────────────────────────────────────────────
def load_last_sync() -> dict:
    if os.path.exists(LAST_SYNC_FILE):
        with open(LAST_SYNC_FILE) as f:
            return json.load(f)
    return {"last_sync_ts": None, "last_memory_ids": {}}

def save_last_sync(state: dict, dry_run: bool = False):
    state["last_sync_ts"] = datetime.now(timezone.utc).isoformat()
    if not dry_run:
        os.makedirs(os.path.dirname(LAST_SYNC_FILE), exist_ok=True)
        with open(LAST_SYNC_FILE, "w") as f:
            json.dump(state, f, indent=2)

# ── Database queries ───────────────────────────────────────────────────────────
def load_memories(conn: sqlite3.Connection, agent_id: str, instance: str,
                  min_id: int = 0) -> list[dict]:
    """Load memories for one agent, optionally from a minimum ID."""
    rows = conn.execute("""
        SELECT id, domain, memory_type, importance, content, source_ts, ts_source
        FROM consolidated
        WHERE agent_id = ? AND instance = ? AND id > ?
        ORDER BY id ASC
    """, (agent_id, instance, min_id)).fetchall()
    return [
        {
            "id": r[0], "domain": r[1], "memory_type": r[2],
            "importance": r[3], "content": r[4],
            "source_ts": r[5], "ts_source": r[6],
        }
        for r in rows
    ]

def count_memories(conn: sqlite3.Connection, agent_id: str, instance: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM consolidated WHERE agent_id = ? AND instance = ?",
        (agent_id, instance)
    ).fetchone()[0]

def get_max_id(conn: sqlite3.Connection, agent_id: str, instance: str) -> int:
    r = conn.execute(
        "SELECT MAX(id) FROM consolidated WHERE agent_id = ? AND instance = ?",
        (agent_id, instance)
    ).fetchone()
    return r[0] or 0

# ── Agent page builder ─────────────────────────────────────────────────────────
def build_agent_page(agent_id: str, instance: str, display_name: str,
                     all_memories: list[dict]) -> str:
    """Build the full Markdown content for an agent's wiki page."""
    total = len(all_memories)
    now = fmt_date(datetime.now().isoformat())

    semantic = [m for m in all_memories if m["memory_type"] in ("semantic", "claim")]
    episodic = [m for m in all_memories if m["memory_type"] == "episodic"]
    dream_mems = [m for m in all_memories if "dream" in (m["ts_source"] or "")]

    # Collect all topics this agent touches
    all_topics = set()
    for m in all_memories:
        for t in detect_topics(m["content"]):
            all_topics.add(t)

    # Collect all project keywords
    project_links = set()
    for m in all_memories:
        lower = m["content"].lower()
        if "hashi" in lower: project_links.add("[[Projects/HASHI]]")
        if "minato" in lower: project_links.add("[[Projects/Minato]]")
        if "nagare" in lower: project_links.add("[[Projects/Nagare]]")
        if "lily-remote" in lower or "hashi remote" in lower:
            project_links.add("[[Projects/Lily_Remote]]")
        if "florence" in lower: project_links.add("[[Projects/Florence2]]")

    lines = [
        f"---",
        f"type: agent",
        f"name: {agent_id}",
        f"display_name: {display_name}",
        f"instance: {instance}",
        f"total_memories: {total}",
        f"last_updated: {now}",
        f"tags: [agent, {instance.lower()}]",
        f"---",
        f"",
        f"# {display_name} ({agent_id})",
        f"",
        f"**实例:** {instance} · **总记忆:** {total} 条 · **更新:** {now}",
        f"",
    ]

    # Projects section
    if project_links:
        lines += [
            f"## 参与的项目",
            f"",
        ]
        for p in sorted(project_links):
            lines.append(f"- {p}")
        lines.append("")

    # Topics section
    if all_topics:
        lines += [
            f"## 涉及主题",
            f"",
        ]
        for t in sorted(all_topics):
            lines.append(f"- [[Topics/{t}]]")
        lines.append("")

    # Semantic memories (durable knowledge)
    if semantic:
        lines += [
            f"## 持久知识 (Semantic)",
            f"",
        ]
        for m in semantic[-50:]:  # cap at 50
            date = fmt_date(m["source_ts"])
            backlink = source_to_backlink(m["ts_source"] or "")
            bl_str = f" · {backlink}" if backlink else ""
            lines.append(f"- [{date}]{bl_str} {truncate(m['content'], 120)}")
        lines.append("")

    # Recent episodic memories (table, capped at 30)
    recent_episodic = episodic[-30:]
    if recent_episodic:
        lines += [
            f"## 近期 Episodic 记忆（最近 {len(recent_episodic)} 条）",
            f"",
            f"| 日期 | 来源 | 内容摘要 |",
            f"|------|------|---------|",
        ]
        for m in reversed(recent_episodic):
            date = fmt_date(m["source_ts"])
            src = m["ts_source"] or m["domain"] or "—"
            backlink = source_to_backlink(src)
            src_display = backlink if backlink else f"`{src}`"
            lines.append(f"| {date} | {src_display} | {truncate(m['content'], 80)} |")
        lines.append("")

    # Dream references
    dream_sources = set()
    for m in all_memories:
        src = m["ts_source"] or ""
        if src.startswith("dream:"):
            dream_sources.add(src[6:])
    if dream_sources:
        lines += [
            f"## Dream 来源",
            f"",
        ]
        for d in sorted(dream_sources, reverse=True)[:10]:
            lines.append(f"- [[Dreams/{d}]]")
        lines.append("")

    return "\n".join(lines)

# ── Daily page builder ─────────────────────────────────────────────────────────
def build_daily_pages(conn: sqlite3.Connection, dry_run: bool, min_id: int = 0):
    """Generate Daily/<date>.md files grouping all memories by date across all agents."""
    rows = conn.execute("""
        SELECT id, agent_id, instance, domain, memory_type, content, source_ts, ts_source
        FROM consolidated
        WHERE id > ?
        ORDER BY source_ts ASC
    """, (min_id,)).fetchall()

    # Group by date
    by_date: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        date = fmt_date(row[6])
        agent_id = row[1]
        by_date[date][agent_id].append({
            "domain": row[3], "memory_type": row[4], "content": row[5],
            "source_ts": row[6], "ts_source": row[7],
        })

    created = 0
    for date, agents in sorted(by_date.items()):
        path = f"{VAULT_ROOT}/Daily/{date}.md"
        total_day = sum(len(v) for v in agents.values())

        lines = [
            f"---",
            f"type: daily",
            f"date: {date}",
            f"total_memories: {total_day}",
            f"tags: [daily]",
            f"---",
            f"",
            f"# 记忆日志 — {date}",
            f"",
            f"**当日记忆总计:** {total_day} 条 · 涉及 {len(agents)} 个 agents",
            f"",
        ]
        for agent_id in sorted(agents.keys()):
            mems = agents[agent_id]
            dname, inst = AGENT_MAP.get(agent_id, (agent_id, "?"))
            lines += [
                f"## [[Agents/{agent_id}|{dname}]] ({len(mems)} 条)",
                f"",
            ]
            for m in mems:
                src = m["ts_source"] or ""
                backlink = source_to_backlink(src)
                bl_str = f" · {backlink}" if backlink else (f" · `{src}`" if src and src != "native" else "")
                domain = m["domain"] or "personal"
                lines.append(f"- **[{domain}]**{bl_str} {truncate(m['content'], 150)}")
            lines.append("")

        write_file(path, "\n".join(lines), dry_run)
        created += 1

    return created

# ── Topic page updater ─────────────────────────────────────────────────────────
def update_topic_pages(topic_memories: dict[str, list[dict]], dry_run: bool):
    """For each topic, append new memories to its page (create if missing)."""
    for topic, entries in topic_memories.items():
        path = f"{VAULT_ROOT}/Topics/{topic}.md"
        if not os.path.exists(path):
            # Create new topic page
            content = f"""---
type: topic
name: {topic}
last_updated: {fmt_date(datetime.now().isoformat())}
tags: [topic]
---

# {topic.replace('_', ' ')}

_自动从 agent 记忆提取_

## 相关记忆

"""
            write_file(path, content, dry_run)

        # Append new entries
        lines = []
        for e in entries:
            date = fmt_date(e["source_ts"])
            agent_link = f"[[Agents/{e['agent_id']}]]"
            src = e.get("ts_source") or ""
            backlink = source_to_backlink(src)
            bl_str = f" · {backlink}" if backlink else ""
            lines.append(f"- [{date}] {agent_link}{bl_str} {truncate(e['content'], 100)}\n")

        if lines:
            append_to_file(path, "".join(lines), dry_run)

# ── Dream page builder ─────────────────────────────────────────────────────────
def update_dream_pages(dream_data: dict[str, dict], dry_run: bool):
    """Create or update a dream page for each dream date found."""
    for dream_date, data in dream_data.items():
        path = f"{VAULT_ROOT}/Dreams/{dream_date}.md"
        if os.path.exists(path):
            continue  # Don't overwrite existing dream pages

        agents_involved = sorted(data["agents"].keys())
        total_new = sum(data["agents"].values())

        agent_links = " · ".join(f"[[Agents/{a}]]" for a in agents_involved)
        lines = [
            f"---",
            f"type: dream",
            f"date: {dream_date}",
            f"total_new_memories: {total_new}",
            f"tags: [dream]",
            f"---",
            f"",
            f"# Dream — {dream_date}",
            f"",
            f"**参与 Agents:** {agent_links}",
            f"**新增记忆总计:** {total_new} 条",
            f"",
            f"## 各 Agent 新增明细",
            f"",
        ]
        for agent, count in sorted(data["agents"].items(), key=lambda x: -x[1]):
            lines.append(f"- [[Agents/{agent}]]: +{count} 条")
        lines.append("")

        write_file(path, "\n".join(lines), dry_run)

# ── Agent index updater ────────────────────────────────────────────────────────
def update_agent_index(stats: dict, dry_run: bool):
    """Rewrite Agents/_Index.md with current memory counts."""
    now = fmt_date(datetime.now().isoformat())
    lines = [
        f"# Agent 花名册",
        f"",
        f"> 自动同步自 HASHI · 上次更新：{now}",
        f"",
        f"---",
        f"",
    ]
    for inst, agents in AGENTS.items():
        lines += [f"## {inst}", f"", f"| Agent | 名字 | 记忆数 | 上次活跃 |",
                  f"|-------|------|--------|---------|"]
        for aid, dname in agents:
            key = f"{inst}:{aid}"
            count = stats.get(key, {}).get("total", 0)
            last = stats.get(key, {}).get("last_ts", "—")
            lines.append(f"| [[{aid}]] | {dname} | {count} | {last} |")
        lines.append("")

    write_file(f"{VAULT_ROOT}/Agents/_Index.md", "\n".join(lines), dry_run)

# ── Home page stats updater ────────────────────────────────────────────────────
def update_home_stats(total_memories: int, total_agents: int, now: str, dry_run: bool):
    path = f"{VAULT_ROOT}/00_Home.md"
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        content = f.read()
    stats_block = (
        f"## 统计\n\n"
        f"- **总记忆数:** {total_memories:,} 条\n"
        f"- **活跃 Agents:** {total_agents}\n"
        f"- **上次同步:** {now}\n"
    )
    # Replace the stats block
    content = re.sub(
        r"## 统计\n.*?(?=\n---|\n##|\Z)",
        stats_block,
        content, flags=re.DOTALL
    )
    write_file(path, content, dry_run)

# ── Email reports sync ─────────────────────────────────────────────────────────
def sync_email_reports(dry_run: bool) -> int:
    """Copy new email report files from sunny's workspace to Obsidian vault."""
    if not os.path.exists(EMAIL_REPORTS_DIR):
        return 0
    os.makedirs(VAULT_EMAIL_DIR, exist_ok=True)

    # Build index page listing all reports
    report_files = sorted(
        f for f in os.listdir(EMAIL_REPORTS_DIR) if f.endswith(".md")
    )
    copied = 0
    for fname in report_files:
        src = os.path.join(EMAIL_REPORTS_DIR, fname)
        dst = os.path.join(VAULT_EMAIL_DIR, fname)
        # Only copy if dest doesn't exist or source is newer
        if not os.path.exists(dst) or os.path.getmtime(src) > os.path.getmtime(dst):
            if not dry_run:
                import shutil
                shutil.copy2(src, dst)
            else:
                print(f"[DRY] Would copy email report: {fname}")
            copied += 1

    # Rebuild index
    if report_files and not dry_run:
        lines = [
            "# 邮件扫描报告 索引",
            "",
            "> 小夏每日扫描 · 只记录重要和紧急邮件",
            "",
            "| 日期 | 链接 |",
            "|------|------|",
        ]
        for fname in reversed(report_files):
            date = fname.replace(".md", "")
            lines.append(f"| {date} | [[EmailReports/{fname.replace('.md','')}]] |")
        lines.append("")
        write_file(f"{VAULT_EMAIL_DIR}/_Index.md", "\n".join(lines), dry_run)

    return copied

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    full_export = "--full" in sys.argv
    dry_run     = "--dry-run" in sys.argv

    if not os.path.exists(CONSOLIDATED_DB):
        print(f"ERROR: consolidated_memory.sqlite not found at {CONSOLIDATED_DB}")
        sys.exit(1)

    conn = sqlite3.connect(CONSOLIDATED_DB)
    sync_state = load_last_sync()
    last_ids = sync_state.get("last_memory_ids", {})

    if full_export:
        last_ids = {}
        print("Mode: FULL EXPORT (all memories)")
    else:
        print("Mode: INCREMENTAL (new memories only)")

    now_str = fmt_date(datetime.now().isoformat())
    total_exported = 0
    total_agents_active = 0
    agent_stats = {}
    topic_memories: dict[str, list[dict]] = defaultdict(list)
    dream_data: dict[str, dict] = {}

    print(f"\nVault: {VAULT_ROOT}")
    print(f"{'─'*60}")

    for inst, agents in AGENTS.items():
        print(f"\n[{inst}]")
        for agent_id, display_name in agents:
            key = f"{inst}:{agent_id}"
            min_id = 0 if full_export else last_ids.get(key, 0)

            new_memories = load_memories(conn, agent_id, inst, min_id=min_id)
            total_count  = count_memories(conn, agent_id, inst)
            max_id       = get_max_id(conn, agent_id, inst)

            # Determine last activity
            last_ts = "—"
            if new_memories:
                last_ts = fmt_date(new_memories[-1]["source_ts"])
            elif total_count > 0:
                r = conn.execute(
                    "SELECT MAX(source_ts) FROM consolidated WHERE agent_id=? AND instance=?",
                    (agent_id, inst)
                ).fetchone()
                last_ts = fmt_date(r[0]) if r and r[0] else "—"

            agent_stats[key] = {"total": total_count, "last_ts": last_ts}

            new_count = len(new_memories)
            print(f"  {agent_id:12s} total={total_count:4d}  new={new_count:4d}  last={last_ts}")

            if new_count == 0 and not full_export:
                last_ids[key] = max_id
                continue

            total_exported += new_count
            total_agents_active += 1

            # Build agent page (full rebuild if full_export, else incremental append)
            if full_export:
                all_mems = load_memories(conn, agent_id, inst, min_id=0)
            else:
                # For incremental: load all memories to rebuild the full page
                # (Agent pages are always fully rewritten to reflect current stats)
                all_mems = load_memories(conn, agent_id, inst, min_id=0)

            agent_page = build_agent_page(agent_id, inst, display_name, all_mems)
            write_file(f"{VAULT_ROOT}/Agents/{agent_id}.md", agent_page, dry_run)

            # Collect topic data from NEW memories only
            for m in new_memories:
                m["agent_id"] = agent_id
                topics = detect_topics(m["content"])
                for t in topics:
                    topic_memories[t].append(m)

                # Collect dream data
                src = m["ts_source"] or ""
                if src.startswith("dream:"):
                    ddate = src[6:]
                    if ddate not in dream_data:
                        dream_data[ddate] = {"agents": {}}
                    dream_data[ddate]["agents"][agent_id] = \
                        dream_data[ddate]["agents"].get(agent_id, 0) + 1

            last_ids[key] = max_id

    # Write topic pages
    if topic_memories:
        print(f"\n[Topics] Updating {len(topic_memories)} topic pages...")
        update_topic_pages(topic_memories, dry_run)
        for t, mems in topic_memories.items():
            print(f"  {t}: +{len(mems)} entries")

    # Write dream pages
    if dream_data:
        print(f"\n[Dreams] Creating/updating {len(dream_data)} dream pages...")
        update_dream_pages(dream_data, dry_run)

    # Write daily pages (full history by date)
    global_min_id = min(last_ids.values()) if last_ids and not full_export else 0
    print(f"\n[Daily] Building daily memory logs...")
    daily_count = build_daily_pages(conn, dry_run, min_id=global_min_id if not full_export else 0)

    conn.close()

    # Sync email reports
    print(f"\n[EmailReports] Syncing sunny's email reports...")
    email_count = sync_email_reports(dry_run)
    print(f"  Copied/updated: {email_count} report(s)")

    # Update index and home
    print(f"\n[Meta] Updating _Index and 00_Home...")
    update_agent_index(agent_stats, dry_run)
    total_all = sum(v["total"] for v in agent_stats.values())
    update_home_stats(total_all, total_agents_active, now_str, dry_run)

    # Save sync state
    sync_state["last_memory_ids"] = last_ids
    save_last_sync(sync_state, dry_run)

    # Summary
    print(f"\n{'─'*60}")
    print(f"Export complete.")
    print(f"  Agents processed : {len(AGENT_MAP)}")
    print(f"  New memories     : {total_exported:,}")
    print(f"  Topics updated   : {len(topic_memories)}")
    print(f"  Dream pages      : {len(dream_data)}")
    print(f"  Daily pages      : {daily_count}")
    print(f"  Email reports    : {email_count}")
    print(f"  Vault            : {VAULT_ROOT}")
    if dry_run:
        print("  [DRY RUN — no files written]")

if __name__ == "__main__":
    main()
