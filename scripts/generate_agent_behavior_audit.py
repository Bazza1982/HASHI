#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
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
TASKS_JSON = PROJECT_ROOT / "tasks.json"
AGENTS_JSON = PROJECT_ROOT / "agents.json"
BRIDGE_LAUNCH_SCRIPT = PROJECT_ROOT / "bin" / "bridge-u.sh"
BRIDGE_LOG = PROJECT_ROOT / "logs" / "bridge.log"
LOGS_ROOT = PROJECT_ROOT / "logs"
MANAGED_HEARTBEATS_JSON = PROJECT_ROOT / "managed_active_heartbeats.json"
TRANSCRIPT_GLOB = PROJECT_ROOT / "workspaces" / "*" / "transcript.jsonl"

NETWORK_PATTERNS: dict[str, str] = {
    "requests/httpx/aiohttp": r"\b(?:requests|httpx|aiohttp)\b",
    "urllib": r"\burllib(?:\.request)?\b",
    "localhost": r"(?:127\.0\.0\.1|localhost)",
    "chat completions": r"(?:/v1/|/api/chat|chat/completions)",
}

DIRECT_BACKEND_PATTERNS: dict[str, str] = {
    "backend adapter": r"\bget_backend_class\b",
    "active backend env": r"\bBRIDGE_ACTIVE_BACKEND\b",
    "config manager": r"\bConfigManager\b",
}


@dataclass(frozen=True)
class CronEntry:
    cron_id: str
    agent: str
    schedule: str
    action: str
    note: str
    enabled: bool
    backend: str | None
    args: str | None = None
    prompt: str | None = None


@dataclass(frozen=True)
class SkillAudit:
    skill_name: str
    script_path: Path | None
    fanout: list[CronEntry]
    risky_hits: list[str]
    direct_hits: list[str]
    secondary_hits: list[str]
    secondary_paths: list[str]
    missing: bool = False


@dataclass(frozen=True)
class Finding:
    severity: str
    title: str
    details: list[str]
    evidence: list[str]


TRANSCRIPT_NEGATIVE_PATTERNS = [
    ("违令/纠正", re.compile(r"(不对|没让你|谁让你|重新报告|重报|不是这个|你忘了|你没|you did not|you didn't|I did not ask|I didn't ask|who told you|not what I asked)", re.I)),
]
ASSISTANT_COMPLETION_PATTERN = re.compile(r"(完成|已修复|已解决|ready|resolved|没问题)", re.I)
ASSISTANT_SPECULATION_PATTERN = re.compile(r"(应该|大概|看起来|可能是)", re.I)
ASSISTANT_PROACTIVE_ACTION_PATTERN = re.compile(
    r"(我已经|我先|我直接|我刚刚已经|I already|I've already|I went ahead|I just|I'll just)",
    re.I,
)
CONSOLIDATED_BEHAVIOR_PATTERNS = {
    "越权/自行决定": re.compile(r"(未经授权|自行决定|without (?:asking|authorization)|替爸爸做决定)", re.I),
    "先做后报": re.compile(r"(先做后报|先做|做了再说)", re.I),
    "scope drift/未授权测试": re.compile(r"(scope drift|未经授权测试|擅自测试|胡乱测试)", re.I),
}


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


def bullet(lines: Iterable[str]) -> str:
    return "\n".join(f"- {line}" for line in lines)


def load_json(path: Path, fallback: object) -> object:
    if not path.exists():
        return fallback
    return json.loads(path.read_text(encoding="utf-8"))


def parse_frontmatter_run(skill_md: Path) -> str | None:
    if not skill_md.exists():
        return None
    text = skill_md.read_text(encoding="utf-8", errors="ignore")
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    for line in parts[1].splitlines():
        if line.startswith("run:"):
            return line.split(":", 1)[1].strip()
    return None


def line_hits(text: str, patterns: dict[str, str]) -> list[str]:
    hits: list[str] = []
    lines = text.splitlines()
    for label, pattern in patterns.items():
        for idx, line in enumerate(lines, start=1):
            if re.search(r'^\s*["\'][^"\']+["\']\s*:\s*r["\']', line):
                continue
            if re.search(pattern, line):
                snippet = " ".join(line.strip().split())
                if len(snippet) > 100:
                    snippet = snippet[:97] + "..."
                hits.append(f"{label} @ line {idx}: `{snippet}`")
                break
    return hits


def discover_secondary_script_paths(script_path: Path, text: str) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for candidate in re.findall(r'["\']([A-Za-z0-9_./-]+\.py)["\']', text):
        path = Path(candidate)
        choices = []
        if path.is_absolute():
            choices.append(path)
        else:
            choices.append(script_path.parent / path)
            choices.append(PROJECT_ROOT / path)
            choices.append(PROJECT_ROOT / "scripts" / path.name)
        for choice in choices:
            resolved = choice.resolve()
            if resolved.exists() and resolved != script_path.resolve() and resolved not in seen:
                found.append(resolved)
                seen.add(resolved)
                break
    return found


def load_agents() -> dict[str, dict]:
    obj = load_json(AGENTS_JSON, {"agents": []})
    agents = obj.get("agents", []) if isinstance(obj, dict) else []
    return {str(agent.get("name")): agent for agent in agents if agent.get("name")}


def load_enabled_crons(agent_map: dict[str, dict]) -> list[CronEntry]:
    obj = load_json(TASKS_JSON, {"crons": []})
    crons = obj.get("crons", []) if isinstance(obj, dict) else []
    items: list[CronEntry] = []
    for cron in crons:
        if not cron.get("enabled"):
            continue
        agent = str(cron.get("agent") or "")
        items.append(
            CronEntry(
                cron_id=str(cron.get("id") or ""),
                agent=agent,
                schedule=str(cron.get("schedule") or ""),
                action=str(cron.get("action") or ""),
                note=str(cron.get("note") or ""),
                enabled=bool(cron.get("enabled")),
                backend=(agent_map.get(agent) or {}).get("active_backend"),
                args=str(cron.get("args")) if cron.get("args") is not None else None,
                prompt=str(cron.get("prompt")) if cron.get("prompt") is not None else None,
            )
        )
    return items


def audit_job_config(crons: list[CronEntry]) -> list[Finding]:
    findings: list[Finding] = []
    target = next((cron for cron in crons if cron.cron_id == "lily-daily-agent-audit"), None)
    if target is None:
        findings.append(
            Finding(
                severity="High",
                title="审计 job 缺失",
                details=["`lily-daily-agent-audit` 不在 enabled cron 列表中。"],
                evidence=[f"检查文件：{TASKS_JSON}"],
            )
        )
        return findings

    if target.action != "skill:agent_audit":
        findings.append(
            Finding(
                severity="Critical",
                title="审计 job 仍是提示词型执行，不是本地审计 skill",
                details=[
                    f"`lily-daily-agent-audit` 当前 action 为 `{target.action}`。",
                    "这会让审计依赖 agent 自述，而不是固定的本地证据检查流程。",
                ],
                evidence=[
                    f"{TASKS_JSON}: cron id `lily-daily-agent-audit`",
                    "期望配置：`action=skill:agent_audit`",
                ],
            )
        )
    return findings


def resolve_skill_script(skill_name: str) -> Path | None:
    skill_dir = PROJECT_ROOT / "skills" / skill_name
    skill_md = skill_dir / "skill.md"
    run_name = parse_frontmatter_run(skill_md) or f"{skill_name}.py"
    script_path = skill_dir / run_name
    return script_path if script_path.exists() else None


def audit_enabled_skills(crons: list[CronEntry]) -> list[SkillAudit]:
    fanout_map: dict[str, list[CronEntry]] = {}
    for cron in crons:
        if cron.action.startswith("skill:"):
            fanout_map.setdefault(cron.action.split(":", 1)[1], []).append(cron)

    audits: list[SkillAudit] = []
    for skill_name, fanout in sorted(fanout_map.items()):
        script_path = resolve_skill_script(skill_name)
        if script_path is None:
            audits.append(
                SkillAudit(
                    skill_name=skill_name,
                    script_path=None,
                    fanout=fanout,
                    risky_hits=[],
                    direct_hits=[],
                    secondary_hits=[],
                    secondary_paths=[],
                    missing=True,
                )
            )
            continue
        text = script_path.read_text(encoding="utf-8", errors="ignore")
        secondary_paths = discover_secondary_script_paths(script_path, text)
        secondary_hits: list[str] = []
        if skill_name != "agent_audit":
            for secondary_path in secondary_paths:
                secondary_text = secondary_path.read_text(encoding="utf-8", errors="ignore")
                secondary_hits.extend(
                    [
                        f"{secondary_path}: {hit}"
                        for hit in line_hits(secondary_text, NETWORK_PATTERNS)
                    ]
                )
        else:
            secondary_paths = []
        audits.append(
            SkillAudit(
                skill_name=skill_name,
                script_path=script_path,
                fanout=fanout,
                risky_hits=line_hits(text, NETWORK_PATTERNS),
                direct_hits=line_hits(text, DIRECT_BACKEND_PATTERNS),
                secondary_hits=secondary_hits,
                secondary_paths=[str(path) for path in secondary_paths],
            )
        )
    return audits


def heartbeat_findings() -> list[Finding]:
    findings: list[Finding] = []
    tasks = load_json(TASKS_JSON, {"heartbeats": []})
    tasks_heartbeats = tasks.get("heartbeats", []) if isinstance(tasks, dict) else []
    managed = load_json(MANAGED_HEARTBEATS_JSON, {"heartbeats": []})
    managed_heartbeats = managed.get("heartbeats", []) if isinstance(managed, dict) else []
    managed_ids = {str(item.get("id")) for item in managed_heartbeats if item.get("id")}
    duplicate_ids = [
        str(item.get("id"))
        for item in tasks_heartbeats
        if item.get("id") and str(item.get("id")) in managed_ids
    ]
    if duplicate_ids:
        findings.append(
            Finding(
                severity="High",
                title="managed heartbeat 再次出现双真源",
                details=[f"重复 heartbeat id: `{item}`" for item in duplicate_ids],
                evidence=[
                    f"{TASKS_JSON}",
                    f"{MANAGED_HEARTBEATS_JSON}",
                ],
            )
        )
    return findings


def credential_findings(crons: list[CronEntry]) -> list[Finding]:
    findings: list[Finding] = []
    keyword_pattern = re.compile(r"(?i)\b(password|api[_-]?key|secret|app password|access token|bearer token)\b")
    secret_like_pattern = re.compile(r"\b[a-z]{16}\b")
    for cron in crons:
        if cron.action != "enqueue_prompt":
            continue
        payload = "\n".join(part for part in [cron.prompt or "", cron.args or ""] if part)
        if not payload:
            continue
        hits: list[str] = []
        for match in keyword_pattern.finditer(payload):
            hits.append(match.group(1))
        if secret_like_pattern.search(payload):
            hits.append("16-char lowercase secret-like string")
        unique_hits = sorted(set(hits))
        if unique_hits:
            findings.append(
                Finding(
                    severity="High",
                    title=f"`{cron.cron_id}` 的 enqueue_prompt 含 credential 暴露特征",
                    details=[
                        f"agent=`{cron.agent}`，该 cron 通过自由文本把敏感凭据或凭据语义注入 agent 上下文。",
                        f"命中特征：{', '.join(unique_hits)}",
                    ],
                    evidence=[f"{TASKS_JSON}: cron `{cron.cron_id}` 的 prompt/args"],
                )
            )
    return findings


def runtime_backend_mismatch_findings(agent_map: dict[str, dict]) -> list[Finding]:
    findings: list[Finding] = []
    for agent_name, agent in sorted(agent_map.items()):
        if agent_name == "lily":
            continue
        agent_log_root = LOGS_ROOT / agent_name
        if not agent_log_root.exists():
            continue
        runs = sorted(path for path in agent_log_root.iterdir() if path.is_dir())
        if not runs:
            continue
        latest = runs[-1]
        observed: set[str] = set()
        evidence: list[str] = []
        for log_name in ["maintenance.log", "events.log"]:
            log_path = latest / log_name
            if not log_path.exists():
                continue
            for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                if "source=scheduler" not in line and "Cron Task [" not in line:
                    continue
                m = re.search(r"engine='([^']+)'", line)
                if m:
                    observed.add(m.group(1))
                    evidence.append(f"{log_path}: {line}")
                m2 = re.search(r"via ([a-z0-9_-]+cli|openrouter-api|deepseek-api)", line)
                if m2:
                    observed.add(m2.group(1))
                    evidence.append(f"{log_path}: {line}")
        configured = agent.get("active_backend")
        if configured and observed and configured not in observed:
            findings.append(
                Finding(
                    severity="Medium",
                    title=f"`{agent_name}` 配置 backend 与最近 scheduler 运行 engine 不一致",
                    details=[
                        f"`agents.json` 配置为 `{configured}`，但最近日志观察到 `{', '.join(sorted(observed))}`。",
                        "这不自动等于故障；如果存在 runtime override，这可能是预期行为，但不能再把配置值当成已确认运行值。",
                    ],
                    evidence=evidence[:3] + [f"{AGENTS_JSON}: agent `{agent_name}` active_backend=`{configured}`"],
                )
            )
    return findings


def api_usage_findings(crons: list[CronEntry], skill_audits: list[SkillAudit]) -> tuple[list[Finding], list[str], list[str]]:
    findings: list[Finding] = []
    clean_notes: list[str] = []
    approved_notes: list[str] = []

    remote_crons = [
        cron
        for cron in crons
        if cron.action == "enqueue_prompt" and cron.backend in {"openrouter-api", "deepseek-api"}
    ]
    if remote_crons:
        findings.append(
            Finding(
                severity="Critical",
                title="enabled cron 仍绑定未批准远程 API backend",
                details=[
                    f"`{cron.cron_id}` -> `{cron.backend}` ({cron.agent})" for cron in remote_crons
                ],
                evidence=[f"{TASKS_JSON}", f"{AGENTS_JSON}"],
            )
        )
    else:
        clean_notes.append("已检查 enabled `enqueue_prompt` cron，对应 agent backend 中未发现 `openrouter-api` 或 `deepseek-api`。")

    risky_skill_audits = [audit for audit in skill_audits if audit.risky_hits]
    if risky_skill_audits:
        for audit in risky_skill_audits:
            severity = "High" if len(audit.fanout) > 1 else "Medium"
            fanout = ", ".join(f"{cron.agent}/{cron.cron_id}" for cron in audit.fanout)
            findings.append(
                Finding(
                    severity=severity,
                    title=f"`skill:{audit.skill_name}` 存在 API hop 风险特征",
                    details=[
                        f"影响 cron: {fanout}",
                        "风险特征命中不等于已确认违规，但必须人工复核实现意图。",
                    ],
                    evidence=[
                        f"{audit.script_path}",
                        *audit.risky_hits,
                    ],
                )
            )
    else:
        clean_notes.append("已检查所有 enabled `skill:*` 的实现文件，未发现 `requests/httpx/aiohttp/urllib/localhost/127.0.0.1//v1/` 这类 API hop 特征。")

    secondary_risky = [audit for audit in skill_audits if audit.secondary_hits]
    for audit in secondary_risky:
        fanout = ", ".join(f"{cron.agent}/{cron.cron_id}" for cron in audit.fanout)
        findings.append(
            Finding(
                severity="High" if len(audit.fanout) > 1 else "Medium",
                title=f"`skill:{audit.skill_name}` 的二级脚本出现 API hop 风险特征",
                details=[
                    f"影响 cron: {fanout}",
                    "这是 wrapper 之外的 subprocess/import 目标脚本命中，不应再被顶层扫描遗漏。",
                ],
                evidence=audit.secondary_hits[:4],
            )
        )

    missing_skills = [audit for audit in skill_audits if audit.missing]
    if missing_skills:
        findings.append(
            Finding(
                severity="High",
                title="enabled skill 缺少可审计实现文件",
                details=[f"`skill:{audit.skill_name}` 未解析到本地脚本" for audit in missing_skills],
                evidence=[f"{PROJECT_ROOT / 'skills'}"],
            )
        )

    if BRIDGE_LAUNCH_SCRIPT.exists():
        launch_text = BRIDGE_LAUNCH_SCRIPT.read_text(encoding="utf-8", errors="ignore")
        if "/api/chat" in launch_text:
            approved_notes.append("`bin/bridge-u.sh` 仍包含 onboarding startup injector 的 `/api/chat` 路径；按现有批准属于例外，除非范围变化。")
    return findings, clean_notes, approved_notes


def recent_scheduler_lines(limit: int = 8) -> list[str]:
    if not BRIDGE_LOG.exists():
        return ["缺少 bridge.log，无法复核 scheduler 触发轨迹。"]
    lines = BRIDGE_LOG.read_text(encoding="utf-8", errors="ignore").splitlines()
    today = now_sydney().strftime("%Y-%m-%d")
    matched = [line for line in lines if today in line and "BridgeU.Scheduler" in line]
    return matched[-limit:] if matched else ["最近 24 小时没有捕获到 scheduler 触发记录。"]


def latest_lily_log_dir() -> Path | None:
    root = LOGS_ROOT / "lily"
    if not root.exists():
        return None
    dirs = sorted(path for path in root.iterdir() if path.is_dir())
    return dirs[-1] if dirs else None


def recent_lily_log_summary() -> list[str]:
    latest = latest_lily_log_dir()
    if latest is None:
        return ["缺少 `logs/lily/*` 目录。"]
    lines: list[str] = [f"检查运行日志目录：{latest}"]
    for name in ["events.log", "errors.log", "maintenance.log", "telegram.log"]:
        path = latest / name
        if not path.exists():
            lines.append(f"`{name}` 缺失")
            continue
        tail = path.read_text(encoding="utf-8", errors="ignore").splitlines()[-1:]
        if tail:
            lines.append(f"`{name}` 最新记录：{tail[0]}")
    return lines


def consolidated_stats() -> tuple[int, list[str], list[str]]:
    if not CONSOLIDATED_DB.exists():
        return 0, [], []
    cutoff = now_sydney() - timedelta(hours=24)
    cutoff_text = cutoff.isoformat()
    with sqlite3.connect(CONSOLIDATED_DB) as conn:
        total_agents = fetch_all(
            conn,
            "SELECT agent_id, COUNT(*) FROM consolidated GROUP BY agent_id ORDER BY agent_id",
        )
        recent_agents = fetch_all(
            conn,
            "SELECT agent_id, COUNT(*) FROM consolidated WHERE source_ts >= ? GROUP BY agent_id ORDER BY COUNT(*) DESC, agent_id",
            (cutoff_text,),
        )
    collected = [str(agent_id) for agent_id, _ in total_agents]
    recent = [f"{agent_id}: {count} 条" for agent_id, count in recent_agents]
    return len(collected), collected, recent


def transcript_coverage(collected_agents: list[str]) -> tuple[list[str], list[str]]:
    checked: list[str] = []
    missing: list[str] = []
    for agent in collected_agents:
        path = PROJECT_ROOT / "workspaces" / agent / "transcript.jsonl"
        if path.exists():
            checked.append(f"{agent}: `{path}`")
        else:
            missing.append(f"{agent}: transcript 缺失")
    return checked, missing


def bridge_memory_stats() -> str:
    if not BRIDGE_DB.exists():
        return "缺少 `bridge_memory.sqlite`。"
    with sqlite3.connect(BRIDGE_DB) as conn:
        row = fetch_one(conn, "SELECT COUNT(*), MAX(ts) FROM memories")
    assert row is not None
    return f"`bridge_memory.sqlite` 记录数 {int(row[0])}，最新 ts: {row[1]}"


def parse_recent_transcript_entries(agent: str, limit: int = 200) -> list[dict]:
    path = PROJECT_ROOT / "workspaces" / agent / "transcript.jsonl"
    if not path.exists():
        return []
    cutoff = now_sydney() - timedelta(hours=24)
    if datetime.fromtimestamp(path.stat().st_mtime, tz=cutoff.tzinfo) < cutoff:
        return []
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()[-limit:]
    entries: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        entries.append(obj)
    return entries


def is_behavior_relevant_user_turn(entry: dict) -> bool:
    source = str(entry.get("source") or "")
    text = str(entry.get("text") or "")
    if source not in {"", "text"}:
        return False
    stripped = text.strip()
    disallowed_prefixes = (
        "[hchat from",
        "--- SKILL CONTEXT",
        "--- AGENT FYI ---",
        "[HCHAT TASK]",
        "[SYSTEM]",
        "Loop 任务",
        "[🔄 LOOP TASK",
        "--- RECENT CONTEXT ---",
        "--- ADDITIONAL SYSTEM CONTEXT ---",
        "<system-reminder>",
    )
    return not any(stripped.startswith(prefix) for prefix in disallowed_prefixes)


def is_behavior_relevant_assistant_turn(entry: dict) -> bool:
    source = str(entry.get("source") or "")
    return source in {"", "text"}


def classify_correction_signal(user_text: str, assistant_text: str) -> tuple[str, str, str]:
    user_lower = user_text.lower()
    if "i did not ask" in user_lower or "i didn't ask" in user_lower or "没让你" in user_text or "谁让你" in user_text:
        return (
            "High",
            "未经请求的主动动作",
            "agent 在没有用户明确请求的情况下主动做了动作或输出，随后被用户直接追问来源。",
        )
    if "why did you do that" in user_lower or "你为什么这么做" in user_text or "为什么" in user_text and "做" in user_text:
        return (
            "High",
            "未经授权操作",
            "用户不是在纠正措辞，而是在追问 agent 为什么擅自执行了动作，属于更强的越权信号。",
        )
    if "重新报告" in user_text or "重报" in user_text:
        return (
            "High" if ASSISTANT_COMPLETION_PATTERN.search(assistant_text) else "Medium",
            "重报/补报要求",
            "agent 的前一轮输出没有满足要求，被用户要求重做或重报。",
        )
    if ASSISTANT_COMPLETION_PATTERN.search(assistant_text):
        return (
            "High",
            "假完成/假确认",
            "agent 先给出完成或确认式表述，随后被用户直接纠正。",
        )
    if ASSISTANT_SPECULATION_PATTERN.search(assistant_text):
        return (
            "Medium",
            "把猜测说成结论",
            "agent 先以推测性表述回应，随后被用户直接纠正，说明存在证据强度过头的问题。",
        )
    return (
        "Medium",
        "违令/纠正信号",
        "用户近期直接纠正或否定了 agent，说明存在高置信度行为失准证据。",
    )


def user_turn_looks_like_action_request(text: str) -> bool:
    markers = [
        "请", "yes", "do that", "please do that", "next batch", "go for it", "let's do", "i agree",
        "continue", "move", "负责", "修理", "执行", "检查", "读取", "写", "修改", "查", "整理", "生成",
        "report", "fix", "scan", "read", "review", "set up", "负责修理", "迁过去",
    ]
    lowered = text.lower()
    return any(marker in text or marker in lowered for marker in markers)


def user_turn_is_question_or_uncertainty(text: str) -> bool:
    lowered = text.lower()
    markers = ["?", "？", "why", "how", "i guess", "can i", "could i", "should i", "是不是", "可以吗", "为什么", "怎么"]
    return any(marker in text or marker in lowered for marker in markers)


def transcript_behavior_findings(collected_agents: list[str]) -> tuple[list[Finding], list[str]]:
    findings: list[Finding] = []
    notes: list[str] = []
    for agent in collected_agents:
        entries = parse_recent_transcript_entries(agent)
        if not entries:
            continue
        notes.append(f"{agent}: transcript 最近样本 {len(entries)} 条")
        agent_count = 0
        seen_keys: set[tuple[str, str]] = set()
        for idx, entry in enumerate(entries):
            role = str(entry.get("role") or "")
            text = str(entry.get("text") or "")
            if role != "user" or not is_behavior_relevant_user_turn(entry):
                continue
            neg_label = None
            for label, pattern in TRANSCRIPT_NEGATIVE_PATTERNS:
                if pattern.search(text):
                    neg_label = label
                    break
            if not neg_label:
                continue
            prev_assistant = None
            for back in range(idx - 1, -1, -1):
                if (
                    str(entries[back].get("role") or "") == "assistant"
                    and is_behavior_relevant_assistant_turn(entries[back])
                ):
                    prev_assistant = entries[back]
                    break
            if prev_assistant is None:
                continue
            prev_text = str(prev_assistant.get("text") or "")
            if len(prev_text.strip()) < 20:
                continue
            severity, category, detail = classify_correction_signal(text, prev_text)
            title = f"`{agent}` transcript 出现{category}"
            key = (category, prev_text[:80])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            findings.append(
                Finding(
                    severity=severity,
                    title=title,
                    details=[detail],
                    evidence=[
                        f"assistant: {prev_text[:180]}",
                        f"user: {text[:180]}",
                    ],
                )
            )
            agent_count += 1
            if agent_count >= 3:
                break
        if agent_count < 3:
            for idx, entry in enumerate(entries):
                if str(entry.get("role") or "") != "assistant" or not is_behavior_relevant_assistant_turn(entry):
                    continue
                text = str(entry.get("text") or "")
                if not ASSISTANT_PROACTIVE_ACTION_PATTERN.search(text):
                    continue
                prev_user = None
                for back in range(idx - 1, -1, -1):
                    if str(entries[back].get("role") or "") == "user" and is_behavior_relevant_user_turn(entries[back]):
                        prev_user = entries[back]
                        break
                if prev_user is None:
                    continue
                prev_user_text = str(prev_user.get("text") or "")
                if user_turn_looks_like_action_request(prev_user_text) or not user_turn_is_question_or_uncertainty(prev_user_text):
                    continue
                key = ("assistant-side proactive action", text[:80])
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                findings.append(
                    Finding(
                        severity="Medium",
                        title=f"`{agent}` transcript 出现主动动作措辞",
                        details=["agent 在没有明显动作指令的前文下使用了“我已经/我先/我直接”类表述，需要人工确认是否存在先做后报。"],
                        evidence=[
                            f"user: {prev_user_text[:180]}",
                            f"assistant: {text[:180]}",
                        ],
                    )
                )
                agent_count += 1
                if agent_count >= 3:
                    break
    return findings, notes


def consolidated_behavior_findings() -> tuple[list[Finding], list[str]]:
    if not CONSOLIDATED_DB.exists():
        return [], []
    cutoff = (now_sydney() - timedelta(hours=24)).isoformat()
    findings: list[Finding] = []
    notes: list[str] = []
    with sqlite3.connect(CONSOLIDATED_DB) as conn:
        for label, pattern in CONSOLIDATED_BEHAVIOR_PATTERNS.items():
            rows = fetch_all(
                conn,
                "SELECT agent_id, source_ts, substr(content,1,220) "
                "FROM consolidated WHERE source_ts >= ? ORDER BY source_ts DESC",
                (cutoff,),
            )
            hit = None
            for agent_id, source_ts, snippet in rows:
                if pattern.search(str(snippet)):
                    hit = (str(agent_id), str(source_ts), " ".join(str(snippet).split()))
                    break
            if hit:
                agent_id, source_ts, snippet = hit
                findings.append(
                    Finding(
                        severity="Medium",
                        title=f"`{agent_id}` consolidated 记忆出现行为红旗：{label}",
                        details=["这是记忆层面的行为侧证，需要和 transcript / user feedback 一起看。"],
                        evidence=[f"{source_ts}: {snippet}"],
                    )
                )
            else:
                notes.append(f"最近 24 小时 consolidated 未命中 `{label}` 关键词。")
    return findings, notes


def harmful_behavior_findings(limit: int = 4) -> tuple[list[Finding], list[str]]:
    if not HABIT_DB.exists():
        return [], []
    findings: list[Finding] = []
    notes: list[str] = []
    cutoff = (now_sydney() - timedelta(hours=72)).isoformat()
    with sqlite3.connect(HABIT_DB) as conn:
        rows = fetch_all(
            conn,
            "SELECT agent_id, ts, habit_id, context_summary, feedback_text "
            "FROM habit_events WHERE harmful=1 AND ts >= ? ORDER BY ts DESC LIMIT ?",
            (cutoff, limit),
        )
    seen_habits: set[str] = set()
    for agent_id, ts, habit_id, context_summary, feedback_text in rows:
        if str(habit_id) in seen_habits:
            continue
        seen_habits.add(str(habit_id))
        snippet = " ".join(str(context_summary or feedback_text or "").split())
        if snippet and "[hchat task]" not in snippet.lower() and re.search(r"(you|未授权|did not ask|不该|先做后报|without asking|triggered your last action)", snippet, re.I):
            findings.append(
                Finding(
                    severity="Medium",
                    title=f"`{agent_id}` 最近存在 harmful 行为反馈",
                    details=[f"habit_id=`{habit_id}`，这条 harmful 事件文本本身包含对 agent 行为的负面反馈，不只是抽象计数。"],
                    evidence=[f"{ts}: {snippet[:220]}"],
                )
            )
        else:
            notes.append(f"{agent_id}: recent harmful event at {ts} 未提炼为主 finding（证据方向不足或上下文为空）")
    return findings, notes


def habit_summary(limit: int = 5) -> list[str]:
    if not HABIT_DB.exists():
        return ["缺少 `habit_evaluation.sqlite`。"]
    with sqlite3.connect(HABIT_DB) as conn:
        rows = fetch_all(
            conn,
            "SELECT agent_id, COALESCE(SUM(harmful),0), COALESCE(SUM(triggered),0) "
            "FROM habit_events GROUP BY agent_id "
            "HAVING COALESCE(SUM(harmful),0) > 0 "
            "ORDER BY COALESCE(SUM(harmful),0) DESC, COALESCE(SUM(triggered),0) DESC LIMIT ?",
            (limit,),
        )
    if not rows:
        return ["`habit_evaluation.sqlite` 中暂未看到 harmful>0 的 agent 汇总。"]
    return [f"{agent}: harmful={harmful}, triggered={triggered}" for agent, harmful, triggered in rows]


def sort_findings(findings: list[Finding]) -> list[Finding]:
    order = ["Critical", "High", "Medium", "Medium-Low", "Low"]
    return sorted(findings, key=lambda item: order.index(item.severity) if item.severity in order else 99)


def build_behavior_findings(collected_agents: list[str]) -> tuple[list[Finding], list[str]]:
    findings: list[Finding] = []
    notes: list[str] = []
    transcript_findings, transcript_notes = transcript_behavior_findings(collected_agents)
    consolidated_findings, consolidated_notes = consolidated_behavior_findings()
    harmful_findings, harmful_notes = harmful_behavior_findings()
    findings.extend(transcript_findings)
    findings.extend(consolidated_findings)
    findings.extend(harmful_findings)
    notes.extend(transcript_notes)
    notes.extend(consolidated_notes)
    notes.extend(harmful_notes)
    return sort_findings(findings), notes


def build_hygiene_findings(agent_map: dict[str, dict], crons: list[CronEntry], skill_audits: list[SkillAudit]) -> tuple[list[Finding], list[str], list[str]]:
    findings: list[Finding] = []
    findings.extend(heartbeat_findings())
    findings.extend(credential_findings(crons))
    findings.extend(runtime_backend_mismatch_findings(agent_map))
    api_findings, api_clean, approved_notes = api_usage_findings(crons, skill_audits)
    findings.extend(api_findings)

    direct_backend_notes = []
    for audit in skill_audits:
        if audit.direct_hits and not audit.risky_hits:
            fanout = ", ".join(f"{cron.agent}/{cron.cron_id}" for cron in audit.fanout)
            direct_backend_notes.append(
                f"`skill:{audit.skill_name}` 已直接走 backend adapter（影响 {len(audit.fanout)} 个 cron: {fanout}）。"
            )

    return sort_findings(findings), api_clean + direct_backend_notes, approved_notes


def build_report() -> str:
    generated_at = now_sydney()
    agent_map = load_agents()
    crons = load_enabled_crons(agent_map)
    skill_audits = audit_enabled_skills(crons)
    collected_count, collected_agents, recent_consolidated = consolidated_stats()
    behavior_findings, behavior_notes = build_behavior_findings(collected_agents)
    hygiene_findings, clean_notes, approved_notes = build_hygiene_findings(agent_map, crons, skill_audits)
    self_check_findings = audit_job_config(crons)

    transcript_checked, transcript_missing = transcript_coverage(collected_agents)
    scheduler_lines = recent_scheduler_lines()
    lily_log_lines = recent_lily_log_summary()
    bridge_summary = bridge_memory_stats()
    harmful_lines = habit_summary()

    checked_scope = [
        f"`tasks.json` enabled cron {len(crons)} 条",
        f"`agents.json` 活跃 agent 配置 {len(agent_map)} 条",
        f"`consolidated_memory.sqlite` 历史已收集 agent {collected_count} 个",
        bridge_summary,
        "`bridge.log` 最近 24 小时 scheduler 触发记录",
        "`logs/lily/*/events.log`、`errors.log`、`maintenance.log`、`telegram.log`",
        "最近 24 小时 transcript 内容关键词扫描",
        "最近 24 小时 consolidated 记忆行为关键词检索",
        "所有 enabled `skill:*` 的本地实现链路",
        "共享 skill fanout 映射",
        "enabled `enqueue_prompt` 的 prompt/args credential 关键词扫描",
        "配置 backend 与最近 scheduler 实际 engine 的差异扫描",
        "`habit_evaluation.sqlite` harmful 汇总与最近 harmful 事件抽样",
    ]
    unchecked_scope = []
    if transcript_missing:
        unchecked_scope.append(f"缺少 transcript 的 agent: {', '.join(item.split(':', 1)[0] for item in transcript_missing)}")
    unchecked_scope.append("未自动重建完整 hchat 指令链；目前只通过 transcript/日志侧证，不把这部分写成已确认安全。")
    unchecked_scope.append("`enqueue_prompt` 的实际运行行为仍取决于 agent runtime；本脚本能查 backend/credential 特征，但不能像 `skill:*` 一样直接锁定执行路径。")
    uncertain_scope = [
        "memory 数据库能证明已收集到的记录，不等于能证明所有原始对话都完整入库。",
        "skill 实现扫描能抓 API hop 特征，但不能替代逐个业务语义复核。",
        "`consolidated_memory.sqlite` 的 21 个 agent 是历史全量，不等于最近 24 小时都出现了新证据。",
    ]
    if any(audit.secondary_paths for audit in skill_audits):
        uncertain_scope.append("已追加扫描部分 subprocess/二级脚本，但 import 链仍不是完整静态分析器，不能把这条写成全路径已证实安全。")

    sections: list[str] = []
    sections.append("# 🌸 小蕾每日 Agent 行为审计\n")
    sections.append(f"生成时间：{generated_at.strftime('%Y-%m-%d %H:%M:%S %Z')}\n")

    sections.append("## 今日实际审计证据\n")
    evidence_lines = checked_scope + ["最近 24 小时 scheduler 触发："] + [f"`{line}`" for line in scheduler_lines]
    if recent_consolidated:
        evidence_lines.append("最近 24 小时 consolidated 增量：")
        evidence_lines.extend(recent_consolidated[:8])
    if behavior_notes:
        evidence_lines.append("行为层附加扫描：")
        evidence_lines.extend(behavior_notes[:8])
    evidence_lines.append("harmful 汇总参考：")
    evidence_lines.extend(harmful_lines)
    evidence_lines.extend(lily_log_lines)
    sections.append(bullet(evidence_lines) + "\n")

    sections.append("## 审计覆盖度声明\n")
    sections.append("### 已检查\n")
    sections.append(bullet(checked_scope + transcript_checked[:10]) + "\n")
    sections.append("### 未检查\n")
    sections.append(bullet(unchecked_scope) + "\n")
    sections.append("### 检查了但不能直接确认安全\n")
    sections.append(bullet(uncertain_scope) + "\n")

    sections.append("## 跨 agent 综合审计结论\n")
    if behavior_findings:
        for idx, finding in enumerate(behavior_findings, start=1):
            sections.append(f"{idx}. **[{finding.severity}] {finding.title}**")
            sections.append(bullet(finding.details))
            sections.append("证据：")
            sections.append(bullet(finding.evidence) + "\n")
    else:
        sections.append(
            "今天没有从 transcript / consolidated / harmful 事件里抓到新的高置信度行为失准项，但这只代表本次已检查范围内未发现，并不等于 agent 行为已被证明安全。\n"
        )
    sections.append("## 行为收敛确认\n")
    if clean_notes:
        sections.append(bullet(clean_notes) + "\n")
    else:
        sections.append("- 今天没有新的行为收敛项需要确认。\n")

    sections.append("## 基础设施与治理卫生观察\n")
    if hygiene_findings:
        for idx, finding in enumerate(hygiene_findings, start=1):
            sections.append(f"{idx}. **[{finding.severity}] {finding.title}**")
            sections.append(bullet(finding.details))
            sections.append("证据：")
            sections.append(bullet(finding.evidence) + "\n")
    else:
        sections.append("- 今天没有新的基础设施/治理卫生观察。\n")

    sections.append("## 自动 API 使用检查\n")
    api_lines = [
        f"已检查 enabled cron {len(crons)} 条，其中 `skill:*` {sum(1 for cron in crons if cron.action.startswith('skill:'))} 条，`enqueue_prompt` {sum(1 for cron in crons if cron.action == 'enqueue_prompt')} 条。",
    ]
    if approved_notes:
        api_lines.extend(approved_notes)
    api_lines.append("判定原则：不仅看 cron 配置，还沿着 enabled skill 实现文件与部分二级脚本检查 API hop 特征。")
    api_lines.append("注意：`enqueue_prompt` 仍不是固定脚本路径，本节不能被解读为其运行行为已被完整审计。")
    sections.append(bullet(api_lines) + "\n")

    sections.append("## 执行边界自查（仅 lily / 仅自身 backend / 无其他 agent / 无其他 API）\n")
    boundary_lines = [
        "本次报告由本地 `skill:agent_audit` 脚本生成，不走 OpenRouter、DeepSeek、HASHI API。",
        "未调用其他 agent、sub-agent、hchat 委托来完成本次审计。",
        "本次执行不依赖提示词式自述审计，核心检查逻辑固定在本地脚本。",
    ]
    sections.append(bullet(boundary_lines) + "\n")

    sections.append("## 审计方法自检\n")
    if self_check_findings:
        for idx, finding in enumerate(self_check_findings, start=1):
            sections.append(f"{idx}. **[{finding.severity}] {finding.title}**")
            sections.append(bullet(finding.details))
            sections.append("证据：")
            sections.append(bullet(finding.evidence) + "\n")
    else:
        sections.append("- `lily-daily-agent-audit` 当前仍是 `skill:agent_audit`，没有被改回 `enqueue_prompt`。\n")

    sections.append("## 需要爸爸决定\n")
    decisions: list[str] = []
    if transcript_missing:
        decisions.append("要不要把缺失 transcript 的 agent 也纳入强制证据缺口告警，而不是只记入覆盖度声明？")
    decisions.append("要不要继续把 hchat 日志单独落盘并纳入每日强制检查，避免跨 agent 越权只留在 transcript 侧证里？")
    decisions.append("要不要把 transcript 内容扫描进一步升级成更严格的行为模式规则，例如“先做后报 / 假完成 / 假确认 / 违令”专门分类？")
    if behavior_findings or hygiene_findings:
        decisions.append("今天如果您要，我下一步就继续把同样的行为层检查扩到 `AGENT.md`/`skill.md` 声明与代码一致性 diff，但会放在行为主结论之后。")
    sections.append(bullet(decisions) + "\n")

    return "\n".join(sections).strip() + "\n"


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
    print(f"Audit report written: {path}")
    print(f"Latest report: {LATEST_REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
