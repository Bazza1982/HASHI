"""
Veritas adapter: mineru-extractor
==================================
Extracts text and structure from a PDF.

Extraction strategy (tried in order):
  1. MinerU CLI (magic-pdf) — best structure-aware extraction, optional
  2. PyMuPDF (fitz) — fast, reliable fallback for text-only PDFs
  3. pdfminer.six — pure-Python last resort

Input (task_message.payload.params):
    pdf_path: str  — absolute or repo-relative path to the PDF

Output artifacts:
    extracted_markdown: JSON with full text + page count + extraction_method
    pdf_metadata: JSON with raw PDF metadata
                  NOTE: title/author/DOI from PDF metadata are unreliable —
                  must be validated by an LLM downstream before use in file naming.

Return shape:
    {"status": "completed", "artifacts_produced": {"extracted_markdown": "...", "pdf_metadata": "..."}}
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def mineru_extractor(task_message: dict) -> dict:
    payload = task_message.get("payload", {})
    params = payload.get("params", {})
    run_id = task_message.get("run_id", "unknown")
    step_id = payload.get("step_id", "extract_pdf")

    pdf_path_raw = params.get("pdf_path", "")
    if not pdf_path_raw:
        return {"status": "failed", "error": "pdf_path param is required"}

    pdf_path = Path(pdf_path_raw)
    if not pdf_path.is_absolute():
        pdf_path = Path.cwd() / pdf_path
    if not pdf_path.exists():
        return {"status": "failed", "error": f"PDF not found: {pdf_path}"}

    # Try extraction backends in preference order
    result = _try_mineru(pdf_path)
    if result is None:
        result = _try_pymupdf(pdf_path)
    if result is None:
        result = _try_pdfminer(pdf_path)
    if result is None:
        return {"status": "failed", "error": "No PDF extraction backend available. Install PyMuPDF: pip install pymupdf"}

    md_text, raw_meta, page_count, method = result

    extracted = {
        "pdf_path": str(pdf_path),
        "page_count": page_count,
        "text": md_text,
        "char_count": len(md_text),
        "extraction_method": method,
    }

    metadata = {
        "_warning": "Raw PDF metadata. title/author/DOI are unreliable — validate with LLM before use.",
        "source_pdf": str(pdf_path),
        "page_count": page_count,
        "extraction_method": method,
        "raw": raw_meta,
    }

    artifact_dir = _get_artifact_dir(run_id)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    extracted_path = artifact_dir / f"{step_id}_extracted_markdown.json"
    metadata_path = artifact_dir / f"{step_id}_pdf_metadata.json"

    extracted_path.write_text(json.dumps(extracted, ensure_ascii=False, indent=2), encoding="utf-8")
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info("mineru-extractor: %s extracted %d chars (%s)", pdf_path.name, len(md_text), method)

    return {
        "status": "completed",
        "artifacts_produced": {
            "extracted_markdown": str(extracted_path),
            "pdf_metadata": str(metadata_path),
        },
        "summary": f"Extracted {page_count} pages ({len(md_text)} chars) from {pdf_path.name} via {method}",
    }


# ── Backend 1: MinerU CLI ─────────────────────────────────────────────────────

def _find_magic_pdf() -> str | None:
    """Locate the magic-pdf binary, checking common user-local install paths."""
    import shutil
    import os
    # Augment PATH with ~/.local/bin so pip-installed scripts are found
    extra = os.path.expanduser("~/.local/bin")
    env_path = os.environ.get("PATH", "")
    search_path = f"{extra}:{env_path}" if extra not in env_path else env_path
    found = shutil.which("magic-pdf", path=search_path)
    return found


def _try_mineru(pdf_path: Path) -> tuple | None:
    """Try MinerU CLI extraction. Returns (text, meta, pages, method) or None."""
    try:
        magic_pdf_bin = _find_magic_pdf()
        if not magic_pdf_bin:
            return None

        with tempfile.TemporaryDirectory(prefix="veritas-mineru-") as tmpdir:
            tmp_path = Path(tmpdir)
            output_dir = tmp_path / "output"
            output_dir.mkdir()

            proc = subprocess.run(
                [magic_pdf_bin, "-p", str(pdf_path), "-o", str(output_dir), "-m", "txt"],
                capture_output=True, text=True, timeout=540,
            )
            if proc.returncode != 0:
                logger.debug("MinerU failed (rc=%d): %s", proc.returncode, proc.stderr[:200])
                return None

            pdf_stem = pdf_path.stem
            md_candidates = list(output_dir.rglob(f"{pdf_stem}.md")) + list(output_dir.rglob("*.md"))
            if not md_candidates:
                logger.debug("MinerU produced no .md file")
                return None

            md_text = md_candidates[0].read_text(encoding="utf-8")
            page_count = _estimate_page_count(proc.stdout, md_text)

            raw_meta: dict = {}
            for jf in output_dir.rglob("content_list.json"):
                try:
                    data = json.loads(jf.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and ("metadata" in data or "pdf_info" in data):
                        raw_meta = data.get("metadata") or data.get("pdf_info") or {}
                        break
                except Exception:
                    pass

            return md_text, raw_meta, page_count, "mineru"

    except Exception as exc:
        logger.debug("MinerU unavailable: %s", exc)
        return None


# ── Backend 2: PyMuPDF ────────────────────────────────────────────────────────

def _try_pymupdf(pdf_path: Path) -> tuple | None:
    """Try PyMuPDF extraction. Returns (text, meta, pages, method) or None."""
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(str(pdf_path))
        pages = []
        for i, page in enumerate(doc):
            text = page.get_text("markdown")  # markdown-style text with headings
            if not text.strip():
                text = page.get_text()  # fallback to plain text
            pages.append(f"<!-- page {i+1} -->\n{text}")

        md_text = "\n\n".join(pages)
        meta = dict(doc.metadata)  # title, author, subject, creator, producer, etc.
        page_count = len(doc)
        doc.close()

        return md_text, meta, page_count, "pymupdf"

    except ImportError:
        return None
    except Exception as exc:
        logger.warning("PyMuPDF extraction failed: %s", exc)
        return None


# ── Backend 3: pdfminer.six ──────────────────────────────────────────────────

def _try_pdfminer(pdf_path: Path) -> tuple | None:
    """Try pdfminer.six extraction. Returns (text, meta, pages, method) or None."""
    try:
        from pdfminer.high_level import extract_text
        from pdfminer.pdfpage import PDFPage

        text = extract_text(str(pdf_path))
        # Count pages
        with open(pdf_path, "rb") as f:
            page_count = sum(1 for _ in PDFPage.get_pages(f))

        return text, {}, page_count, "pdfminer"

    except ImportError:
        return None
    except Exception as exc:
        logger.warning("pdfminer extraction failed: %s", exc)
        return None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _estimate_page_count(stdout: str, md_text: str) -> int:
    m = re.search(r"total pages?[:\s]+(\d+)", stdout, re.IGNORECASE)
    if m:
        return int(m.group(1))
    ff_count = md_text.count("\f") + md_text.count("<!-- page break -->")
    return ff_count + 1 if ff_count > 0 else 0


def _get_artifact_dir(run_id: str) -> Path:
    return Path("flow/runs") / run_id / "callable-artifacts"
