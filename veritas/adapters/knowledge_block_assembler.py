"""
Veritas adapter: knowledge-block-assembler
==========================================
Assembles all LLM analysis artifacts into a final Knowledge Block:
  - <slug>.md  — Obsidian-ready markdown with YAML frontmatter
  - <slug>.json — Machine-readable record for RAG / LLM ingestion

The .json format is versioned (schema_version: "1.0") for forward compatibility.
New fields can be added freely; consumers should treat unknown keys as optional.

Input (task_message.payload.input_artifacts):
    classification        — paper_type, title, authors, year, journal, doi, etc.
    abstract_analysis     — one-sentence summary, keywords
    intro_analysis        — research gap, problem statement, positioning
    litreview_analysis    — key_theories, conversation_partners, theoretical_lens
    core_analysis         — type-specific deep analysis
    discussion_analysis   — main_findings, implications, contributions
    limitations_analysis  — author-stated + independently identified
    citation_map          — key_references, conversation_cluster, follow-up suggestions
    research_integration  — relevance_score, integration_points, recommended_citations
    pdf_metadata          — raw metadata (for provenance only, NOT for naming)
    extracted_markdown    — full PDF text (included as raw_text chunk in JSON)

Output artifacts:
    knowledge_block_md    — .md file path
    knowledge_block_json  — .json file path
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = "1.0"


def knowledge_block_assembler(task_message: dict) -> dict:
    payload = task_message.get("payload", {})
    run_id = task_message.get("run_id", "unknown")
    step_id = payload.get("step_id", "assemble_knowledge_block")
    input_artifacts = payload.get("input_artifacts", {})

    # Load all analysis artifacts
    data = {}
    for key, path in input_artifacts.items():
        try:
            data[key] = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Could not read artifact %s from %s: %s", key, path, exc)
            data[key] = {}

    classification = data.get("classification", {})

    # LLM-validated title/authors/doi from classifier (this IS reliable, unlike MinerU raw)
    title = classification.get("title", "Untitled")
    authors = classification.get("authors", [])
    year = str(classification.get("year", ""))
    journal = classification.get("journal", "")
    doi = classification.get("doi", "")
    paper_type = classification.get("paper_type", "unknown")
    core_contribution = classification.get("core_contribution", "")

    # Build file slug from LLM-validated metadata
    first_author_last = _extract_last_name(authors[0]) if authors else "unknown"
    slug = _make_slug(first_author_last, year, title)

    # Build .md and .json
    md_content = _build_markdown(slug, title, authors, year, journal, doi, paper_type,
                                 core_contribution, data)
    json_content = _build_json(slug, title, authors, year, journal, doi, paper_type,
                               core_contribution, data)

    # Write outputs
    artifact_dir = _get_artifact_dir(run_id)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    md_path = artifact_dir / f"{slug}.md"
    json_path = artifact_dir / f"{slug}.json"

    md_path.write_text(md_content, encoding="utf-8")
    json_path.write_text(json.dumps(json_content, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info("knowledge-block-assembler: created %s (.md + .json)", slug)

    return {
        "status": "completed",
        "artifacts_produced": {
            "knowledge_block_md": str(md_path),
            "knowledge_block_json": str(json_path),
        },
        "summary": f"Knowledge Block assembled: {slug}",
    }


def _build_markdown(slug, title, authors, year, journal, doi, paper_type,
                    core_contribution, data: dict) -> str:
    abstract_a = data.get("abstract_analysis", {})
    intro_a = data.get("intro_analysis", {})
    litreview_a = data.get("litreview_analysis", {})
    core_a = data.get("core_analysis", {})
    discussion_a = data.get("discussion_analysis", {})
    limits_a = data.get("limitations_analysis", {})
    citation_a = data.get("citation_map", {})
    integration = data.get("research_integration", {})

    authors_str = "; ".join(authors) if authors else ""
    keywords = abstract_a.get("keywords", [])
    keywords_yaml = "\n  - ".join([""] + keywords) if keywords else ""
    tags_yaml = f"\n  - {paper_type}\n  - veritas\n  - academic"
    relevance = integration.get("relevance_score", "")
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    frontmatter = f"""---
title: "{_yaml_escape(title)}"
authors: {_yaml_list(authors)}
year: {year}
journal: "{_yaml_escape(journal)}"
doi: "{doi}"
paper_type: {paper_type}
core_contribution: "{_yaml_escape(core_contribution)}"
relevance: {relevance}
keywords:{keywords_yaml}
tags:{tags_yaml}
ingested_at: {now_iso}
schema_version: "{_SCHEMA_VERSION}"
---"""

    sections = [frontmatter, f"\n# {title}\n"]

    if authors_str:
        sections.append(f"**Authors:** {authors_str}  \n**Year:** {year}  \n**Journal:** {journal}  \n**DOI:** {doi}\n")

    if core_contribution:
        sections.append(f"## Core Contribution\n\n{core_contribution}\n")

    if abstract_a.get("original_abstract"):
        sections.append(f"## Abstract\n\n{abstract_a['original_abstract']}\n")

    if abstract_a.get("one_sentence_summary"):
        sections.append(f"## One-Sentence Summary\n\n> {abstract_a['one_sentence_summary']}\n")

    if intro_a.get("research_gap"):
        sections.append(f"## Research Gap\n\n{intro_a['research_gap']}\n")
    if intro_a.get("problem_statement"):
        sections.append(f"## Research Question\n\n{intro_a['problem_statement']}\n")

    if litreview_a.get("key_theories"):
        sections.append(f"## Theoretical Framework\n\n{_format_list(litreview_a['key_theories'])}\n")

    if core_a:
        sections.append(f"## Core Analysis ({paper_type.title()})\n\n{_format_dict(core_a)}\n")

    if discussion_a.get("main_findings"):
        sections.append(f"## Key Findings\n\n{_format_list(discussion_a['main_findings'])}\n")
    if discussion_a.get("theoretical_implications"):
        sections.append(f"## Theoretical Implications\n\n{discussion_a['theoretical_implications']}\n")
    if discussion_a.get("practical_implications"):
        sections.append(f"## Practical Implications\n\n{discussion_a['practical_implications']}\n")

    if limits_a.get("author_stated_limitations"):
        sections.append(f"## Limitations (Author-Stated)\n\n{_format_list(limits_a['author_stated_limitations'])}\n")
    if limits_a.get("independently_identified"):
        sections.append(f"## Limitations (Independent Assessment)\n\n{_format_list(limits_a['independently_identified'])}\n")
    if limits_a.get("caution_notes"):
        sections.append(f"## Citation Cautions\n\n{_format_list(limits_a['caution_notes'])}\n")

    if citation_a.get("key_references"):
        refs = citation_a["key_references"]
        ref_lines = []
        for r in refs[:10]:
            if isinstance(r, dict):
                ref_lines.append(f"- {r.get('citation', '')} — *{r.get('relationship', '')}*")
            else:
                ref_lines.append(f"- {r}")
        sections.append("## Key References\n\n" + "\n".join(ref_lines) + "\n")

    if integration.get("integration_points"):
        sections.append(f"## Research Integration\n\n**Relevance:** {relevance}\n\n{_format_dict(integration.get('integration_points', {}))}\n")
    if integration.get("recommended_citations"):
        sections.append(f"## Ready-to-Use Citations\n\n{_format_list(integration['recommended_citations'])}\n")
    if integration.get("ai_prompt_templates"):
        templates = integration["ai_prompt_templates"]
        tmpl_lines = "\n\n".join(f"```\n{t}\n```" for t in templates) if isinstance(templates, list) else str(templates)
        sections.append(f"## AI Prompt Templates\n\n{tmpl_lines}\n")

    return "\n".join(sections)


def _build_json(slug, title, authors, year, journal, doi, paper_type,
                core_contribution, data: dict) -> dict:
    """Build the machine-readable JSON for RAG/LLM ingestion."""
    abstract_a = data.get("abstract_analysis", {})
    integration = data.get("research_integration", {})
    extracted = data.get("extracted_markdown", {})

    chunks = []

    # Chunk 0: bibliographic header (always included, small, high-density)
    chunks.append({
        "chunk_id": f"{slug}_meta",
        "type": "metadata",
        "content": (
            f"Title: {title}\n"
            f"Authors: {', '.join(authors)}\n"
            f"Year: {year}\nJournal: {journal}\nDOI: {doi}\n"
            f"Type: {paper_type}\n"
            f"Core contribution: {core_contribution}\n"
            f"Relevance: {integration.get('relevance_score', '')}"
        ),
    })

    # Chunk 1: abstract + one-sentence summary
    if abstract_a:
        chunks.append({
            "chunk_id": f"{slug}_abstract",
            "type": "abstract",
            "content": (
                abstract_a.get("original_abstract", "")
                + "\n\nSummary: " + abstract_a.get("one_sentence_summary", "")
            ).strip(),
        })

    # Chunk 2+: remaining analysis sections as individual chunks
    _add_json_chunk(chunks, slug, data, "intro_analysis", "introduction")
    _add_json_chunk(chunks, slug, data, "litreview_analysis", "literature_review")
    _add_json_chunk(chunks, slug, data, "core_analysis", "core_analysis")
    _add_json_chunk(chunks, slug, data, "discussion_analysis", "discussion")
    _add_json_chunk(chunks, slug, data, "limitations_analysis", "limitations")
    _add_json_chunk(chunks, slug, data, "citation_map", "citations")
    _add_json_chunk(chunks, slug, data, "research_integration", "integration")

    # Raw text chunk (large — for full-text retrieval)
    raw_text = extracted.get("text", "") if isinstance(extracted, dict) else ""
    if raw_text:
        chunks.append({
            "chunk_id": f"{slug}_raw_text",
            "type": "raw_pdf_text",
            "content": raw_text[:50000],  # cap at 50k chars to avoid bloat
        })

    return {
        "schema_version": _SCHEMA_VERSION,
        "slug": slug,
        "title": title,
        "authors": authors,
        "year": year,
        "journal": journal,
        "doi": doi,
        "paper_type": paper_type,
        "core_contribution": core_contribution,
        "relevance_score": integration.get("relevance_score", ""),
        "keywords": data.get("abstract_analysis", {}).get("keywords", []),
        "avoid_citations": integration.get("avoid_citations", []),
        "chunks": chunks,
    }


def _add_json_chunk(chunks: list, slug: str, data: dict, key: str, chunk_type: str) -> None:
    val = data.get(key)
    if not val:
        return
    content = json.dumps(val, ensure_ascii=False) if isinstance(val, (dict, list)) else str(val)
    chunks.append({
        "chunk_id": f"{slug}_{chunk_type}",
        "type": chunk_type,
        "content": content,
    })


def _make_slug(first_author_last: str, year: str, title: str) -> str:
    """Generate a short, filesystem-safe slug: AuthorYear_TitleWords"""
    title_words = re.sub(r"[^\w\s]", "", title).split()[:4]
    title_part = "_".join(w.lower() for w in title_words if w.lower() not in
                          {"a", "an", "the", "of", "in", "on", "for", "and", "or"})
    base = f"{first_author_last}{year}_{title_part}" if year else f"{first_author_last}_{title_part}"
    return re.sub(r"[^\w\-]", "_", base)[:80]


def _extract_last_name(author: str) -> str:
    """Extract last name from 'First Last' or 'Last, First' format."""
    author = author.strip()
    if "," in author:
        return re.sub(r"[^\w]", "", author.split(",")[0]).lower()
    parts = author.split()
    return re.sub(r"[^\w]", "", parts[-1]).lower() if parts else "unknown"


def _yaml_escape(s: str) -> str:
    return str(s).replace('"', '\\"')


def _yaml_list(items: list) -> str:
    if not items:
        return "[]"
    return "\n  - " + "\n  - ".join(str(i) for i in items)


def _format_list(items) -> str:
    if isinstance(items, list):
        return "\n".join(f"- {item}" for item in items)
    return str(items)


def _format_dict(d) -> str:
    if isinstance(d, dict):
        lines = []
        for k, v in d.items():
            v_str = _format_list(v) if isinstance(v, list) else str(v)
            lines.append(f"**{k}:** {v_str}")
        return "\n\n".join(lines)
    return str(d)


def _get_artifact_dir(run_id: str) -> Path:
    return Path("flow/runs") / run_id / "callable-artifacts"
