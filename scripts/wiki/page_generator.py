"""Dry-run topic page generation from classified wiki state."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .config import TOPICS, WikiConfig
from .fetcher import has_private_content, has_sensitive_content


@dataclass(frozen=True)
class ClassifiedMemory:
    consolidated_id: int
    topic_id: str
    confidence: float
    agent_id: str
    instance: str
    domain: str
    memory_type: str
    content: str
    source_ts: str
    ts_source: str


@dataclass(frozen=True)
class PageDraft:
    topic_id: str
    path: Path
    memory_count: int


def generate_dry_run_pages(
    config: WikiConfig,
    *,
    min_confidence: float = 0.7,
    max_memories_per_topic: int = 30,
) -> list[PageDraft]:
    config.dry_run_pages_dir.mkdir(parents=True, exist_ok=True)
    drafts: list[PageDraft] = []
    for topic_id in sorted(TOPICS):
        if topic_id == "NONE":
            continue
        memories = fetch_topic_memories(
            config,
            topic_id,
            min_confidence=min_confidence,
            limit=max_memories_per_topic,
        )
        if not memories:
            continue
        content = build_topic_page(topic_id, memories)
        path = config.dry_run_pages_dir / "Topics" / f"{topic_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        drafts.append(PageDraft(topic_id=topic_id, path=path, memory_count=len(memories)))
    return drafts


def fetch_topic_memories(
    config: WikiConfig,
    topic_id: str,
    *,
    min_confidence: float = 0.7,
    limit: int = 30,
) -> list[ClassifiedMemory]:
    if not config.wiki_state_db.exists():
        return []
    if not config.consolidated_db.exists():
        return []
    con = sqlite3.connect(config.wiki_state_db)
    con.row_factory = sqlite3.Row
    con.execute("ATTACH DATABASE ? AS mem", (str(config.consolidated_db),))
    rows = con.execute(
        """
        SELECT
            a.consolidated_id,
            a.topic_id,
            a.confidence,
            c.agent_id,
            c.instance,
            c.domain,
            c.memory_type,
            c.content,
            c.source_ts,
            c.ts_source
        FROM classification_assignment AS a
        JOIN mem.consolidated AS c ON c.id = a.consolidated_id
        WHERE a.topic_id = ?
          AND a.status = 'ok'
          AND a.confidence >= ?
        ORDER BY c.source_ts DESC, a.consolidated_id DESC
        LIMIT ?
        """,
        (topic_id, min_confidence, limit),
    ).fetchall()
    con.close()
    return [
        ClassifiedMemory(
            consolidated_id=int(row["consolidated_id"]),
            topic_id=row["topic_id"],
            confidence=float(row["confidence"]),
            agent_id=row["agent_id"],
            instance=row["instance"],
            domain=row["domain"],
            memory_type=row["memory_type"],
            content=sanitize_page_content(row["content"]),
            source_ts=row["source_ts"],
            ts_source=row["ts_source"],
        )
        for row in rows
    ]


def build_topic_page(topic_id: str, memories: list[ClassifiedMemory]) -> str:
    meta = TOPICS[topic_id]
    lines = [
        "---",
        f'topic_id: "{topic_id}"',
        f'title: "{meta["display"]}"',
        "status: dry-run",
        f"memory_count: {len(memories)}",
        "---",
        "",
        f"# {meta['display']}",
        "",
        "<!-- WIKI-GENERATED: dry-run draft; do not treat as final synthesis. -->",
        "",
        "## Scope",
        "",
        meta["desc"],
        "",
        "## Draft Evidence",
        "",
    ]
    for memory in memories:
        lines.extend(
            [
                f"### Memory {memory.consolidated_id}",
                "",
                f"- Agent: `{memory.agent_id}`",
                f"- Instance: `{memory.instance}`",
                f"- Source time: `{memory.source_ts}`",
                f"- Type: `{memory.domain}/{memory.memory_type}`",
                f"- Confidence: `{memory.confidence:.2f}`",
                "",
                truncate_for_page(memory.content),
                "",
                f"<!-- evidence: consolidated_id={memory.consolidated_id}; topic={topic_id} -->",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def sanitize_page_content(content: str) -> str:
    """Apply output-side privacy filtering before writing generated wiki pages."""
    if has_private_content(content) or has_sensitive_content(content):
        return "[private content filtered]"
    return content


def truncate_for_page(content: str, limit: int = 900) -> str:
    compact = " ".join((content or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"
