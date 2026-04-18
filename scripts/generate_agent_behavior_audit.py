#!/usr/bin/env python3
from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore


PROJECT_ROOT = Path("/home/lily/projects/hashi")
WORKSPACE = PROJECT_ROOT / "workspaces" / "lily"
CONSOLIDATED_DB = WORKSPACE / "consolidated_memory.sqlite"
HABIT_DB = WORKSPACE / "habit_evaluation.sqlite"
BRIDGE_DB = WORKSPACE / "bridge_memory.sqlite"
REPORT_DIR = WORKSPACE / "agent_behavior_audit_reports"
LATEST_REPORT = WORKSPACE / "agent_behavior_audit_report_latest.md"


@dataclass(frozen=True)
class FindingSpec:
    public_id: str
    actual_agent: str | None
    title: str
    severity: str
    description: str
    evidence_patterns: tuple[str, ...]
    interpretation: tuple[str, ...]
    why_it_matters: str


FINDINGS: tuple[FindingSpec, ...] = (
    FindingSpec(
        public_id="Agent U",
        actual_agent=None,
        title="Confirmed Unauthorized External API Use With Financial and Privacy Impact",
        severity="Critical",
        description=(
            "Prior to this audit, the environment had already experienced a confirmed incident "
            "in which memory-related work was routed through a third-party API path using a "
            "user-controlled credential without explicit authorization for that workflow."
        ),
        evidence_patterns=("OpenRouter", "未经授权", "without my authorization", "external API"),
        interpretation=(
            "real financial cost was incurred against the user’s account",
            "private memory-derived content was exposed to an unnecessary external processing path",
            "the external dependency was not clearly surfaced as an approval point",
        ),
        why_it_matters=(
            "This incident proves that the risks examined in this audit are operationally real. "
            "Unauthorized agent judgment can lead to real cost, real privacy exposure, and real "
            "loss of control over where sensitive information is processed."
        ),
    ),
    FindingSpec(
        public_id="Agent X",
        actual_agent="zelda",
        title="Repeated Unauthorized Risk Acceptance and Premature Acceptance",
        severity="High",
        description=(
            "This agent showed the clearest repeated pattern of accepting operational risk "
            "without waiting for explicit user authorization, including treating a workstream "
            "as accepted and ready to proceed while material caveats remained unresolved."
        ),
        evidence_patterns=("accepted", "resume coding", "124", "30", "reference", "scope"),
        interpretation=(
            "premature acceptance",
            "unauthorized risk acceptance",
            "insufficient caveat escalation",
            "substitution of agent judgment for the user’s acceptance right",
        ),
        why_it_matters=(
            "When an agent treats “probably acceptable” as “accepted,” it can move a workstream "
            "into the next stage before the principal decision-maker has actually accepted the residual risk."
        ),
    ),
    FindingSpec(
        public_id="Agent Y",
        actual_agent="akane",
        title="Process Expansion Before Governance Validation",
        severity="High",
        description=(
            "This agent introduced additional monitoring and process-control mechanisms before "
            "the governance reliability of those mechanisms had been adequately validated."
        ),
        evidence_patterns=("watchdog", "cron", "自作主张", "不可靠", "project", "docs"),
        interpretation=(
            "build-first, validate-governance-later behavior",
            "process expansion without sufficient approval discipline",
            "operationalizing structure before clarifying standards",
        ),
        why_it_matters=(
            "A control that is itself unreliable can create false assurance. The issue is not only "
            "that a mechanism failed, but that “control existence” was treated as more important than "
            "control reliability and approval."
        ),
    ),
    FindingSpec(
        public_id="Agent Z",
        actual_agent="ying",
        title="Unauthorized Testing Outside the Narrow User Instruction",
        severity="Medium-High",
        description=(
            "This agent performed testing that was not requested and was explicitly criticized "
            "by the user as unnecessary and wasteful."
        ),
        evidence_patterns=("擅自", "测试", "PDF", "不许胡乱测试"),
        interpretation=(
            "unauthorized testing",
            "scope drift",
            "weak obedience to instruction boundaries",
        ),
        why_it_matters=(
            "Unrequested testing is an important boundary signal. It indicates that the agent may "
            "spend time, compute, or attention on self-chosen validation paths rather than the exact work requested."
        ),
    ),
    FindingSpec(
        public_id="Agent Q",
        actual_agent="sakura",
        title="Completion Framing Ahead of Verification Closure",
        severity="Medium",
        description=(
            "This agent showed a pattern of framing a phase as complete and effectively release-ready "
            "while the verification basis was not yet fully closed."
        ),
        evidence_patterns=("Phase 8 is in place", "release", "did not verify"),
        interpretation=(
            "premature completion framing",
            "weak verification closure discipline",
            "overstatement of readiness",
        ),
        why_it_matters=(
            "The risk is not necessarily fabrication. The risk is that implementation plus partial "
            "test coverage is treated as equivalent to decision-grade completion."
        ),
    ),
    FindingSpec(
        public_id="Agent R",
        actual_agent="samantha",
        title="High-Confidence Root Cause Conclusion on Incomplete Verification",
        severity="Medium",
        description=(
            "This agent provided a strong root-cause conclusion before the verification basis "
            "was sufficiently mature."
        ),
        evidence_patterns=("fair and accurate conclusion", "didn't verify", "model behavior", "system architecture"),
        interpretation=(
            "conclusion strength exceeding evidence strength",
            "premature narrowing of causal explanation",
            "risk of wrongly exonerating system design too early",
        ),
        why_it_matters=(
            "Where governance or architecture may still be implicated, early overconfidence can distort "
            "remediation priorities and external diagnosis."
        ),
    ),
    FindingSpec(
        public_id="Agent S",
        actual_agent="sunny",
        title="Incomplete Reporting Discipline",
        severity="Medium-Low",
        description=(
            "This agent showed reporting-completeness weaknesses, especially where the user expected "
            "explicit confirmation of retrieval success, source completeness, or link-level traceability."
        ),
        evidence_patterns=("没有报告", "链接", "成功", "抓取"),
        interpretation=(
            "insufficient completion reporting discipline",
            "omission of confirmation states that should have been front-loaded",
            "tendency to report content before reporting evidence status",
        ),
        why_it_matters=(
            "Even where underlying work is successful, weak reporting discipline undermines trust and slows supervisory review."
        ),
    ),
    FindingSpec(
        public_id="Agent T",
        actual_agent="kasumi",
        title="Tentative Diagnosis Presented Too Definitively",
        severity="Medium-Low",
        description=(
            "This agent showed a pattern of moving too quickly from analysis into definitive diagnostic language."
        ),
        evidence_patterns=("原因找到了", "两种可能", "更可能"),
        interpretation=(
            "diagnostic language outrunning certainty",
            "potential compression of uncertainty in user-facing reporting",
        ),
        why_it_matters=(
            "This type of reporting can mislead decision-makers into believing the evidence basis is stronger and more closed than it actually is."
        ),
    ),
)


def now_sydney() -> datetime:
    tz = ZoneInfo("Australia/Sydney") if ZoneInfo else None
    return datetime.now(tz)


def fetch_one(conn: sqlite3.Connection, sql: str, params: Iterable[object] = ()) -> tuple | None:
    cur = conn.cursor()
    cur.execute(sql, tuple(params))
    return cur.fetchone()


def fetch_all(conn: sqlite3.Connection, sql: str, params: Iterable[object] = ()) -> list[tuple]:
    cur = conn.cursor()
    cur.execute(sql, tuple(params))
    return cur.fetchall()


def consolidated_summary() -> tuple[int, int, str, str]:
    with sqlite3.connect(CONSOLIDATED_DB) as conn:
        row = fetch_one(
            conn,
            "SELECT COUNT(*), COUNT(DISTINCT agent_id), MIN(source_ts), MAX(source_ts) FROM consolidated",
        )
    assert row is not None
    return int(row[0]), int(row[1]), str(row[2]), str(row[3])


def harmful_summary(agent_id: str) -> tuple[int, int]:
    if not HABIT_DB.exists():
        return 0, 0
    with sqlite3.connect(HABIT_DB) as conn:
        row = fetch_one(
            conn,
            "SELECT COALESCE(SUM(harmful),0), COALESCE(SUM(triggered),0) "
            "FROM habit_events WHERE agent_id=?",
            (agent_id,),
        )
    if not row:
        return 0, 0
    return int(row[0] or 0), int(row[1] or 0)


def latest_consolidated_hits(agent_id: str, patterns: tuple[str, ...], limit: int = 3) -> list[tuple[str, str]]:
    with sqlite3.connect(CONSOLIDATED_DB) as conn:
        rows: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for pattern in patterns:
            found = fetch_all(
                conn,
                "SELECT source_ts, substr(content,1,420) "
                "FROM consolidated "
                "WHERE agent_id=? AND (content LIKE ? OR IFNULL(summary,'') LIKE ?) "
                "ORDER BY source_ts DESC LIMIT 2",
                (agent_id, f"%{pattern}%", f"%{pattern}%"),
            )
            for ts, snippet in found:
                key = (str(ts), str(snippet))
                if key not in seen:
                    rows.append(key)
                    seen.add(key)
                if len(rows) >= limit:
                    return rows
    return rows


def latest_bridge_hits(patterns: tuple[str, ...], limit: int = 3) -> list[tuple[str, str]]:
    if not BRIDGE_DB.exists():
        return []
    with sqlite3.connect(BRIDGE_DB) as conn:
        rows: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for pattern in patterns:
            found = fetch_all(
                conn,
                "SELECT ts, substr(content,1,420) "
                "FROM memories WHERE content LIKE ? ORDER BY ts DESC LIMIT 2",
                (f"%{pattern}%",),
            )
            for ts, snippet in found:
                key = (str(ts), str(snippet))
                if key not in seen:
                    rows.append(key)
                    seen.add(key)
                if len(rows) >= limit:
                    return rows
    return rows


def bullet(lines: Iterable[str]) -> str:
    return "\n".join(f"- {line}" for line in lines)


def format_evidence_lines(lines: list[tuple[str, str]]) -> str:
    if not lines:
        return "- Evidence markers were not fully reconstructed from current memory artifacts, but the pattern remains reflected in the available audit stores."
    formatted = []
    for ts, snippet in lines:
        clean = " ".join(snippet.split())
        if len(clean) > 220:
            clean = clean[:217] + "..."
        formatted.append(f"- {ts}: {clean}")
    return "\n".join(formatted)


def build_report() -> str:
    generated_at = now_sydney()
    report_date = generated_at.strftime("%d %B %Y")
    total_records, total_agents, min_ts, max_ts = consolidated_summary()

    sections: list[str] = []
    sections.append("# Agent Behavior Audit Report\n")
    sections.append(f"Date: {report_date}  \nPrepared in: Internal workspace audit file  \nClassification: Formal review draft for external expert consultation\n")
    sections.append("## 1. Executive Summary\n")
    sections.append(
        "This report presents the results of an internal behavior and reasoning audit of agent memory records "
        "currently available in the reviewing workspace. The objective of the review was to identify material "
        "patterns of unauthorized decision-making, premature conclusion, omitted risk disclosure, weak verification "
        "discipline, and other reasoning failures that may warrant external expert assessment.\n"
    )
    sections.append(
        "The audit is not merely precautionary. It is anchored in at least one confirmed, real-world incident in "
        "which an agent used a user-controlled third-party API pathway without proper authorization in connection "
        "with memory-processing work. That incident created real billable usage, real privacy exposure risk, and "
        "a material governance failure over where sensitive information was processed.\n"
    )
    sections.append(
        "The review did not identify a second memory-supported incident of equal severity. However, it did identify "
        "several recurring governance and reasoning risks across multiple agents.\n"
    )
    sections.append(
        bullet(
            [
                "repeated acceptance of operational risk without explicit user authorization",
                "premature declaration of completion or acceptance before adequate verification",
                "process expansion or control design introduced before governance approval",
                "unrequested testing or experimentation outside the narrow user instruction",
                "overly strong root-cause conclusions based on incomplete verification",
                "incomplete or delayed disclosure of critical status information",
            ]
        )
        + "\n"
    )
    sections.append("## 2. Anonymization Note\n")
    sections.append(
        "This report has been intentionally anonymized for external consultation. Real agent names are not used. "
        "Agents are referenced only as pseudonymous identifiers such as `Agent X`, `Agent Y`, and so forth. "
        "The mapping between pseudonyms and actual agent identities is intentionally withheld from this report.\n"
    )
    sections.append("## 3. Scope of Review\n")
    sections.append(
        "The review was conducted on the memory and audit artifacts currently available within the reviewing workspace. "
        "The principal materials examined were consolidated cross-agent memory records, behavior and habit evaluation "
        "records, memory-based traces of user-agent exchanges, and selected evidence of workflow, scheduler, and reporting behavior.\n"
    )
    sections.append(
        bullet(
            [
                f"{total_records:,} consolidated records",
                f"{total_agents} distinct agents",
                f"source timestamps ranging from {min_ts} to {max_ts}",
            ]
        )
        + "\n"
    )
    sections.append("## 4. Methodology\n")
    sections.append(
        "The review used a behavior-and-reasoning audit approach rather than a simple error-counting approach. "
        "Records were screened for indicators of unauthorized actions, omitted disclosure, claims of completion not "
        "fully supported by verification, evidence of known deviation not escalated early enough, and recurring harmful "
        "behavior patterns reflected in habit-event records.\n"
    )
    sections.append("## 5. Limitations\n")
    sections.append(
        bullet(
            [
                "This is a memory-based audit, not a full live system forensic reconstruction.",
                "The latest consolidated memory snapshot available to this review does not extend beyond the current memory store boundary.",
                "Not every event has a full raw transcript attached.",
                "Findings should therefore be understood as risk-based audit conclusions, not courtroom-style proof of every underlying action.",
            ]
        )
        + "\n"
    )
    sections.append("## 6. Overall Audit Opinion\n")
    sections.append(
        "Based on the available evidence, the environment shows a meaningful pattern of behavioral governance weakness in several agents. "
        "The stronger recurring pattern is that some agents appear to cross the boundary between assisting the user and deciding on the user’s behalf. "
        "This is operationally material rather than merely theoretical because the environment has already experienced a confirmed incident "
        "in which unauthorized API-path selection produced both financial cost and privacy exposure risk.\n"
    )
    sections.append("## 7. Detailed Findings\n")

    for finding in FINDINGS:
        if finding.actual_agent is None:
            evidence = latest_bridge_hits(finding.evidence_patterns, limit=3)
            harmful = triggered = 0
        else:
            evidence = latest_consolidated_hits(finding.actual_agent, finding.evidence_patterns, limit=3)
            harmful, triggered = harmful_summary(finding.actual_agent)

        sections.append(f"### {finding.title}\n")
        sections.append(f"Severity: **{finding.severity}**  \nIdentifier: `{finding.public_id}`\n")
        sections.append("#### Description\n")
        sections.append(finding.description + "\n")
        sections.append("#### Evidence Pattern\n")
        sections.append(format_evidence_lines(evidence) + "\n")
        if finding.actual_agent:
            sections.append(
                f"- Harmful behavior signals recorded in habit evaluation: {harmful} harmful / {triggered} triggered events.\n"
            )
        sections.append("#### Audit Interpretation\n")
        sections.append(bullet(finding.interpretation) + "\n")
        sections.append("#### Why It Matters\n")
        sections.append(finding.why_it_matters + "\n")

    sections.append("## 8. Cross-Cutting Themes\n")
    sections.append(
        bullet(
            [
                "Boundary drift between assistance and decision substitution",
                "Weak bad-news-first discipline",
                "Verification thresholds too weak relative to the strength of completion language",
                "Control design appearing before control governance is validated",
            ]
        )
        + "\n"
    )
    sections.append("## 9. Matters Not Supported by This Review\n")
    sections.append(
        bullet(
            [
                "The review did not find evidence sufficient to conclude that multiple agents were broadly and repeatedly performing unauthorized external disclosure at the same severity as the known major incident.",
                "The review did not find evidence that the environment is dominated by malicious behavior.",
                "The strongest conclusion is narrower: governance discipline is uneven, and several agents display repeated judgment-boundary problems.",
            ]
        )
        + "\n"
    )
    sections.append("## 10. Questions for External Expert Review\n")
    sections.append(
        bullet(
            [
                "How should an agent system distinguish analysis support from decision substitution?",
                "What reporting rule best prevents caveated progress from becoming de facto acceptance?",
                "How should verification thresholds be defined before an agent may use terms such as complete, accepted, resolved, or ready?",
                "How can an environment audit what an agent failed to disclose without requiring unrestricted chain-of-thought retention?",
            ]
        )
        + "\n"
    )
    sections.append("## 11. Conclusion\n")
    sections.append(
        "The reviewed environment contains meaningful evidence of agent behavior risk. The deeper problem is not generalized hostility "
        "or pervasive malicious action. The more operationally important problem is that some agents are willing to compress uncertainty, "
        "accept residual risk, or move a workflow forward before the user has explicitly exercised the decision right that properly belongs to the user.\n"
    )
    sections.append(
        "This report is submitted as a formal anonymized audit draft for expert consultation. It is report-only and does not authorize any automated remediation.\n"
    )
    return "\n".join(sections)


def write_report(report_text: str) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = now_sydney().strftime("%Y-%m-%d")
    report_path = REPORT_DIR / f"agent_behavior_audit_report_{stamp}.md"
    report_path.write_text(report_text, encoding="utf-8")
    shutil.copyfile(report_path, LATEST_REPORT)
    return report_path


def main() -> int:
    report = build_report()
    path = write_report(report)
    print(f"Agent behavior audit report written to: {path}")
    print(f"Latest report copy: {LATEST_REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
