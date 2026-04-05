"""
Veritas adapter: vault-writer
==============================
Writes a completed Knowledge Block to the Obsidian vault and maintains
the library index.

Deduplication:
  Before writing, library_index.jsonl is scanned for existing entries
  matching the same DOI (preferred) or title+year combination.
  If a duplicate is found, the step fails with a clear error message so the
  human can decide whether to overwrite (not done automatically).

library_index.jsonl format (one JSON object per line):
  {
    "slug":         "AuthorYear_title_words",
    "title":        "...",
    "authors":      ["..."],
    "year":         "...",
    "journal":      "...",
    "doi":          "...",
    "paper_type":   "quantitative|qualitative|theoretical",
    "relevance":    "high|medium|low",
    "md_path":      "/absolute/path/to/slug.md",
    "json_path":    "/absolute/path/to/slug.json",
    "pdf_path":     "/absolute/path/to/original.pdf",
    "ingested_at":  "2026-04-04T...",
  }

Input (task_message.payload):
    params:
        vault_path: str  — Obsidian vault root (from pre_flight or default)
        pdf_path:   str  — original source PDF path
    input_artifacts:
        knowledge_block_md    — .md file path
        knowledge_block_json  — .json file path
        classification        — JSON with title, authors, year, doi, paper_type

Output artifacts:
    published_md_path   — written .md path
    published_json_path — written .json path
    published_pdf_path  — copied PDF path
    obsidian_uri        — JSON with obsidian:// deep link
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_VAULT_PATH = "/home/lily/obsidian/Research"
_INDEX_FILENAME = "library_index.jsonl"


def vault_writer(task_message: dict) -> dict:
    payload = task_message.get("payload", {})
    params = payload.get("params", {})
    input_artifacts = payload.get("input_artifacts", {})

    vault_path = Path(params.get("vault_path") or _DEFAULT_VAULT_PATH)
    _pdf_str = params.get("pdf_path") or ""
    pdf_source: Path | None = Path(_pdf_str) if _pdf_str else None

    # Read classification for metadata
    classification = _load_json_artifact(input_artifacts, "classification")
    title = classification.get("title", "Untitled")
    authors = classification.get("authors", [])
    year = str(classification.get("year", ""))
    journal = classification.get("journal", "")
    doi = classification.get("doi", "").strip()
    paper_type = classification.get("paper_type", "unknown")

    # Read knowledge block files
    md_source = Path(input_artifacts.get("knowledge_block_md", ""))
    json_source = Path(input_artifacts.get("knowledge_block_json", ""))

    if not md_source.exists():
        return {"status": "failed", "error": f"knowledge_block_md not found: {md_source}"}
    if not json_source.exists():
        return {"status": "failed", "error": f"knowledge_block_json not found: {json_source}"}

    slug = md_source.stem  # e.g. "smith2023_carbon_accounting"

    # Read knowledge block JSON to get relevance_score
    kb_json = _read_json(json_source)
    relevance = kb_json.get("relevance_score", "")

    # --- Ensure vault exists ---
    vault_path.mkdir(parents=True, exist_ok=True)
    index_path = vault_path / _INDEX_FILENAME

    # --- Deduplication check ---
    dup = _find_duplicate(index_path, doi=doi, title=title, year=year)
    if dup:
        return {
            "status": "failed",
            "error": (
                f"Duplicate detected in library_index.jsonl — "
                f"slug='{dup['slug']}' already indexed "
                f"(doi='{dup.get('doi', '')}', title='{dup.get('title', '')}').\n"
                f"If you want to update this entry, delete the existing record first."
            ),
        }

    # --- Write files to vault ---
    dest_md = vault_path / f"{slug}.md"
    dest_json = vault_path / f"{slug}.json"
    shutil.copy2(md_source, dest_md)
    shutil.copy2(json_source, dest_json)

    # Copy source PDF if it exists
    dest_pdf: Path | None = None
    if pdf_source is not None and pdf_source.exists() and pdf_source.is_file():
        dest_pdf = vault_path / f"{slug}{pdf_source.suffix}"
        shutil.copy2(pdf_source, dest_pdf)

    # --- Append to library_index.jsonl ---
    index_entry = {
        "slug": slug,
        "title": title,
        "authors": authors,
        "year": year,
        "journal": journal,
        "doi": doi,
        "paper_type": paper_type,
        "relevance": relevance,
        "md_path": str(dest_md),
        "json_path": str(dest_json),
        "pdf_path": str(dest_pdf) if dest_pdf else "",
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }
    _append_index(index_path, index_entry)

    # --- Build Obsidian URI ---
    vault_name = vault_path.name
    obsidian_uri = f"obsidian://open?vault={vault_name}&file={slug}"

    # Write obsidian_uri artifact
    run_id = task_message.get("run_id", "unknown")
    artifact_dir = _get_artifact_dir(run_id)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    uri_path = artifact_dir / "obsidian_uri.json"
    uri_path.write_text(
        json.dumps({"uri": obsidian_uri, "slug": slug, "vault": str(vault_path)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    logger.info("vault-writer: published %s to %s", slug, vault_path)

    produced = {
        "published_md_path": str(dest_md),
        "published_json_path": str(dest_json),
        "obsidian_uri": str(uri_path),
    }
    if dest_pdf:
        produced["published_pdf_path"] = str(dest_pdf)

    return {
        "status": "completed",
        "artifacts_produced": produced,
        "summary": f"Published {slug} to vault. {obsidian_uri}",
    }


def _find_duplicate(
    index_path: Path,
    *,
    doi: str,
    title: str,
    year: str,
) -> dict | None:
    """
    Scan library_index.jsonl for a duplicate entry.
    Matching priority:
      1. DOI match (if doi is non-empty) — definitive
      2. Normalized title + year match — fallback
    Returns the first matching entry dict, or None.
    """
    if not index_path.exists():
        return None

    norm_title = _normalize_title(title)
    norm_doi = doi.lower().strip() if doi else ""

    with index_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            # DOI match (definitive)
            if norm_doi and entry.get("doi", "").lower().strip() == norm_doi:
                return entry

            # Title + year match (fallback)
            if (
                _normalize_title(entry.get("title", "")) == norm_title
                and str(entry.get("year", "")) == str(year)
            ):
                return entry

    return None


def _append_index(index_path: Path, entry: dict) -> None:
    """Atomically append one JSON line to library_index.jsonl."""
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with index_path.open("a", encoding="utf-8") as fh:
        fh.write(line)


def _normalize_title(title: str) -> str:
    """Lowercase, remove punctuation/stop-words for fuzzy title matching."""
    import re
    t = title.lower()
    t = re.sub(r"[^\w\s]", " ", t)
    stop = {"a", "an", "the", "of", "in", "on", "for", "and", "or", "is", "to"}
    words = [w for w in t.split() if w not in stop]
    return " ".join(words[:8])  # compare first 8 content words


def _load_json_artifact(input_artifacts: dict, key: str) -> dict:
    path = input_artifacts.get(key, "")
    if not path:
        return {}
    return _read_json(Path(path))


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return {}


def _get_artifact_dir(run_id: str) -> Path:
    return Path("flow/runs") / run_id / "callable-artifacts"
