"""Behavioral smoke coverage for the portable Veritas callable adapters."""

import json
from pathlib import Path

from veritas.adapters.knowledge_block_assembler import knowledge_block_assembler
from veritas.adapters.vault_writer import vault_writer

# ── Fake classification (LLM-validated metadata) ─────────────────────────────
FAKE_CLASSIFICATION = {
    "paper_type": "quantitative",
    "title": "Carbon Emission Accounting in Listed Firms: Evidence from Australia",
    "authors": ["Smith, J.", "Li, B.", "Wang, Y."],
    "year": 2023,
    "journal": "Accounting, Auditing & Accountability Journal",
    "doi": "10.1108/AAAJ-01-2023-0001",
    "core_contribution": "Provides first large-sample evidence that Big4-audited firms report significantly lower scope-3 emission intensities.",
    "confidence": "high",
}

FAKE_ABSTRACT = {
    "original_abstract": "This study examines carbon emission accounting practices among ASX-listed firms...",
    "one_sentence_summary": "Big4 auditors are associated with lower reported scope-3 emission intensities in Australian listed firms.",
    "keywords": ["carbon accounting", "scope-3 emissions", "Big4", "Australia", "ESG"],
}

FAKE_INTRO = {
    "research_gap": "Prior literature focuses on scope-1/2 but overlooks scope-3 emission accounting quality.",
    "problem_statement": "Do auditor characteristics affect the reliability of voluntary scope-3 disclosures?",
    "positioning": "Extends voluntary disclosure theory to emissions assurance context.",
    "motivation": "Regulatory pressure from TCFD and ISSB makes this question timely.",
}

FAKE_LITREVIEW = {
    "key_theories": [
        "Voluntary Disclosure Theory",
        "Agency Theory",
        "Institutional Theory",
    ],
    "conversation_partners": [],
    "theoretical_lens": "Positivist",
    "citation_network_summary": "Builds on Clarkson et al. (2008) and Cohen et al. (2011).",
}

FAKE_CORE = {
    "results": "OLS regression: Big4 dummy β=-0.23, p<0.01 (scope-3 intensity).",
    "methodology": "Archival study, 1200 firm-years, 2018–2022, ASX300.",
    "validity": "Controlled for firm size, leverage, industry.",
}

FAKE_DISCUSSION = {
    "main_findings": [
        "Big4 auditors are negatively associated with scope-3 intensity.",
        "Effect is stronger in high-emission industries.",
        "No significant result for scope-1/2 emissions.",
    ],
    "theoretical_implications": "Extends voluntary disclosure theory to GHG reporting.",
    "practical_implications": "Supports mandatory assurance requirements for scope-3.",
    "contribution_to_field": "First Australian large-sample study on audit quality and scope-3.",
}

FAKE_LIMITATIONS = {
    "author_stated_limitations": [
        "Cross-sectional design limits causal inference.",
        "Self-reported emissions data.",
    ],
    "independently_identified": [
        "No control for GHG protocol variation across reporters."
    ],
    "caution_notes": ["Do not cite Big4 β as evidence of causation."],
}

FAKE_CITATIONS = {
    "key_references": [
        {
            "citation": "Clarkson et al. (2008)",
            "relationship": "builds on",
            "why": "Voluntary disclosure framework",
        },
        {
            "citation": "Cohen et al. (2011)",
            "relationship": "extends",
            "why": "Audit quality and CSR",
        },
    ],
    "conversation_cluster": "GHG assurance + voluntary disclosure",
    "suggested_follow_up": ["Simnett et al. (2009)", "Cho & Patten (2007)"],
}

FAKE_INTEGRATION = {
    "relevance_score": "high",
    "integration_points": {
        "theoretical_foundation": "Supports voluntary disclosure framing for Chapters 2-3.",
        "empirical_evidence": "Cite Big4 finding in Chapter 4 as Australian benchmark.",
        "methodological_reference": "Archival design parallels our proposed study.",
    },
    "recommended_citations": [
        "According to Smith et al. (2023), Big4-audited firms report significantly lower scope-3 intensities (p<0.01).",
    ],
    "avoid_citations": ["Do not cite as causal — design is cross-sectional."],
    "ai_prompt_templates": [
        "Summarize Smith et al. (2023)'s findings on Big4 auditors and scope-3 emissions.",
    ],
}

FAKE_PDF_METADATA = {
    "_warning": "Raw MinerU metadata. title/author/DOI are unreliable — validate with LLM before use.",
    "source_pdf": "/tmp/fake_paper.pdf",
    "page_count": 42,
    "raw": {"pdf_title": "Carbon Emission Accounting", "creator": "LaTeX"},
}

FAKE_EXTRACTED = {
    "pdf_path": "/tmp/fake_paper.pdf",
    "page_count": 42,
    "text": "This study examines carbon emission accounting practices among ASX-listed firms...\n[Full text placeholder]",
    "char_count": 87000,
}


def write_artifact(tmp_dir: Path, name: str, data: dict) -> str:
    p = tmp_dir / f"{name}.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(p)


def test_knowledge_block_assembly_vault_publish_and_deduplication(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    monkeypatch.setenv("HASHI_VERITAS_VAULT", str(vault_dir))

    arts = {
        "classification": write_artifact(
            tmp_path, "classification", FAKE_CLASSIFICATION
        ),
        "abstract_analysis": write_artifact(
            tmp_path, "abstract_analysis", FAKE_ABSTRACT
        ),
        "intro_analysis": write_artifact(tmp_path, "intro_analysis", FAKE_INTRO),
        "litreview_analysis": write_artifact(
            tmp_path, "litreview_analysis", FAKE_LITREVIEW
        ),
        "core_analysis": write_artifact(tmp_path, "core_analysis", FAKE_CORE),
        "discussion_analysis": write_artifact(
            tmp_path, "discussion_analysis", FAKE_DISCUSSION
        ),
        "limitations_analysis": write_artifact(
            tmp_path, "limitations_analysis", FAKE_LIMITATIONS
        ),
        "citation_map": write_artifact(tmp_path, "citation_map", FAKE_CITATIONS),
        "research_integration": write_artifact(
            tmp_path, "research_integration", FAKE_INTEGRATION
        ),
        "pdf_metadata": write_artifact(tmp_path, "pdf_metadata", FAKE_PDF_METADATA),
        "extracted_markdown": write_artifact(
            tmp_path, "extracted_markdown", FAKE_EXTRACTED
        ),
    }

    import veritas.adapters.knowledge_block_assembler as kba_mod
    import veritas.adapters.vault_writer as vw_mod

    monkeypatch.setattr(
        kba_mod, "_get_artifact_dir", lambda _run_id: tmp_path / "artifacts"
    )

    task_msg = {
        "run_id": "smoke-001",
        "payload": {
            "step_id": "assemble_knowledge_block",
            "input_artifacts": arts,
            "output_spec": [],
            "params": {},
        },
    }

    result = knowledge_block_assembler(task_msg)

    assert result["status"] == "completed", result
    md_path = Path(result["artifacts_produced"]["knowledge_block_md"])
    json_path = Path(result["artifacts_produced"]["knowledge_block_json"])
    assert md_path.exists()
    assert json_path.exists()

    md_content = md_path.read_text()
    kb_json = json.loads(json_path.read_text())

    assert "schema_version" in kb_json
    assert len(kb_json["chunks"]) >= 3
    assert "---" in md_content

    monkeypatch.setattr(
        vw_mod, "_get_artifact_dir", lambda _run_id: tmp_path / "artifacts"
    )

    vw_task = {
        "run_id": "smoke-001",
        "payload": {
            "step_id": "write_to_vault",
            "params": {
                "pdf_path": "",
            },
            "input_artifacts": {
                "knowledge_block_md": str(md_path),
                "knowledge_block_json": str(json_path),
                "classification": arts["classification"],
            },
            "output_spec": [],
        },
    }

    published = vault_writer(vw_task)

    assert published["status"] == "completed", published
    published_md = Path(published["artifacts_produced"]["published_md_path"])
    assert published_md.exists()

    index_path = vault_dir / "library_index.jsonl"
    assert index_path.exists()
    entries = [
        json.loads(line) for line in index_path.read_text().splitlines() if line.strip()
    ]
    assert len(entries) == 1
    assert entries[0]["doi"] == FAKE_CLASSIFICATION["doi"]

    monkeypatch.setattr(
        vw_mod, "_get_artifact_dir", lambda _run_id: tmp_path / "artifacts2"
    )
    duplicate = vault_writer(vw_task)

    assert duplicate["status"] == "failed"
    assert "Duplicate" in duplicate["error"]
