"""Configuration for the HASHI wiki redesign pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


HASHI_ROOT = Path("/home/lily/projects/hashi")


@dataclass(frozen=True)
class WikiConfig:
    """Runtime paths and policy switches for the wiki pipeline."""

    hashi_root: Path = HASHI_ROOT
    agent_id: str = "lily"
    timezone: str = "Australia/Sydney"
    consolidated_db: Path = HASHI_ROOT / "workspaces/lily/consolidated_memory.sqlite"
    wiki_state_db: Path = HASHI_ROOT / "workspaces/lily/wiki_state.sqlite"
    consolidation_log: Path = HASHI_ROOT / "workspaces/lily/consolidation_log.jsonl"
    report_latest: Path = HASHI_ROOT / "workspaces/lily/wiki_organise_report_latest.md"
    dry_run_report_latest: Path = HASHI_ROOT / "workspaces/lily/wiki_reports/wiki_dry_run_latest.md"
    local_pages_dir: Path = HASHI_ROOT / "workspaces/lily/wiki_pages"
    dry_run_pages_dir: Path = HASHI_ROOT / "workspaces/lily/wiki_pages_dry_run"
    vault_root: Path = Path("/mnt/c/Users/thene/Documents/lily_hashi_wiki")
    min_content_chars: int = 40
    classify_chars: int = 512
    classifier_timeout_s: int = 600
    private_domains: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {"identity", "personal", "relationship", "private", "emotional"}
        )
    )
    approved_cli_backends: frozenset[str] = field(
        default_factory=lambda: frozenset({"claude-cli", "gemini-cli"})
    )


TOPICS: dict[str, dict[str, str]] = {
    "HASHI_Architecture": {
        "display": "HASHI Architecture",
        "desc": "HASHI multi-agent OS, orchestrator, hot-restart, HChat, scheduler, gateway, and agent lifecycle. Security incidents, vulnerability scans, and operational restart procedures belong in HASHI_Ops_Security even when they mention core components. Emotional modeling, Anatta, drive experiments, and persona state belong in Anatta_Emotional_Intelligence.",
    },
    "AI_Memory_Systems": {
        "display": "AI Memory Systems",
        "desc": "Bridge memory, consolidation, embeddings, vector search, SQLite memory schema, and retrieval behavior.",
    },
    "HASHI_Ops_Security": {
        "display": "HASHI Operations & Security",
        "desc": "HASHI operations, routine maintenance, security scans, dependency updates, file permissions, firewall/port review, Windows/WSL operational risk, and safe maintenance actions.",
    },
    "Nagare_Workflow": {
        "display": "Nagare Workflow Engine",
        "desc": "Nagare, Shimanto, HITL workflow steps, approvals, checkpoints, and JobQueue design.",
    },
    "Minato_Platform": {
        "display": "Minato Platform",
        "desc": "Minato agentic AI OS, plugin/socket architecture, Veritas, KASUMI, and AIPM integration.",
    },
    "Dream_System": {
        "display": "Dream & Memory Reflection",
        "desc": "Nightly dream reflection, memory promotion, habit tracking, and dream reports.",
    },
    "Anatta_Emotional_Intelligence": {
        "display": "Anatta Emotional Intelligence",
        "desc": "Anatta layer, EmotionalSelfLayer, DriveContribution, PostTurnObserver, CARE/PLAY/SEEKING drive experiments, emotion-driven response shaping, and persona state modeling.",
    },
    "Obsidian_Wiki": {
        "display": "Obsidian Wiki",
        "desc": "Wiki page design, Obsidian vault layout, backlinks, daily pages, and wiki pipeline behavior.",
    },
    "Carbon_Accounting": {
        "display": "Carbon Accounting Research",
        "desc": "GHG protocol, emissions accounting, sustainability research, and Zelda's carbon/NFIA research materials.",
    },
    "Lily_Remote": {
        "display": "Lily Remote / Hashi Remote",
        "desc": "Remote control app, peer connectivity, remote UI, and Hashi Remote implementation details.",
    },
    "NONE": {
        "display": "Noise / No Wiki Topic",
        "desc": "Infrastructure noise, heartbeats, empty tests, and content with no durable wiki value.",
    },
    "UNCATEGORIZED_REVIEW": {
        "display": "Uncategorized Review",
        "desc": "Significant content that does not fit the current taxonomy and needs human taxonomy review.",
    },
}


def default_config() -> WikiConfig:
    return WikiConfig()
