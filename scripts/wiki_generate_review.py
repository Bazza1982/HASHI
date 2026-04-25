#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path("/home/lily/projects/hashi")
WORKSPACE_DIR = PROJECT_ROOT / "workspaces" / "lily"
VAULT_ROOT = Path("/mnt/c/Users/thene/Documents/lily_hashi_wiki")
DUMP_DIR = WORKSPACE_DIR / "wiki_dump"
STATE_PATH = WORKSPACE_DIR / "state.json"
AGENTS_PATH = PROJECT_ROOT / "agents.json"
SECRETS_PATH = PROJECT_ROOT / "secrets.json"
REPORT_DIR = WORKSPACE_DIR / "wiki_reports"
LATEST_REPORT = WORKSPACE_DIR / "wiki_organise_report_latest.md"
AGENT_NAME = "lily"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from adapters.registry import get_backend_class
from orchestrator.config import AgentConfig, ConfigManager, FlexibleAgentConfig
from orchestrator.flexible_backend_registry import get_secret_lookup_order

SUPPORTED_SAFE_BACKENDS = {"claude-cli", "codex-cli", "gemini-cli"}
_CONFIG_CACHE = None


def _read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def _resolve_backend_context() -> tuple[str, str]:
    env_backend = (os.environ.get("BRIDGE_ACTIVE_BACKEND") or "").strip()
    env_model = (os.environ.get("BRIDGE_ACTIVE_MODEL") or "").strip()
    if env_backend and env_model:
        return env_backend, env_model

    state = _read_json(STATE_PATH, {})
    backend = str(state.get("active_backend") or "").strip()
    model = str(state.get("active_model") or "").strip()
    if backend and model:
        return backend, model

    agents = _read_json(AGENTS_PATH, {}).get("agents", [])
    for entry in agents:
        if str(entry.get("name") or "").strip().lower() != AGENT_NAME:
            continue
        backend = str(entry.get("active_backend") or entry.get("engine") or "").strip()
        model = str(entry.get("model") or "").strip()
        if entry.get("type") == "flex":
            for option in entry.get("allowed_backends") or []:
                if str(option.get("engine") or "").strip() == backend:
                    model = str(option.get("model") or "").strip() or model
                    break
        if backend and model:
            return backend, model
    raise RuntimeError("无法解析 lily 当前 backend/model。")


def _load_runtime_config():
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    manager = ConfigManager(AGENTS_PATH, SECRETS_PATH, bridge_home=PROJECT_ROOT)
    global_cfg, agents, secrets = manager.load()
    agent_cfg = next((a for a in agents if str(getattr(a, "name", "")).lower() == AGENT_NAME), None)
    if agent_cfg is None:
        raise RuntimeError("找不到 lily 的 agent config。")
    _CONFIG_CACHE = (global_cfg, agent_cfg, secrets)
    return _CONFIG_CACHE


def _resolve_api_key_direct(engine: str, secrets: dict) -> str | None:
    for secret_key in get_secret_lookup_order(engine, AGENT_NAME):
        api_key = secrets.get(secret_key)
        if api_key:
            return api_key
    return None


def _build_direct_adapter_config(engine: str, model: str, loaded_agent_cfg):
    if isinstance(loaded_agent_cfg, FlexibleAgentConfig):
        backend_cfg_raw = next((b for b in loaded_agent_cfg.allowed_backends if b["engine"] == engine), None)
        if not backend_cfg_raw:
            raise RuntimeError(f"找不到 backend {engine}")
        agent_extra = dict(getattr(loaded_agent_cfg, "extra", None) or {})
        backend_extra = dict(backend_cfg_raw)
        backend_extra.pop("engine", None)
        backend_extra.pop("model", None)
        backend_scope = backend_cfg_raw.get("access_scope", loaded_agent_cfg.access_scope)
        backend_extra.pop("access_scope", None)
        extra = {**agent_extra, **backend_extra}
        return AgentConfig(
            name=loaded_agent_cfg.name,
            engine=engine,
            workspace_dir=loaded_agent_cfg.workspace_dir,
            system_md=loaded_agent_cfg.system_md,
            model=model,
            is_active=True,
            extra=extra,
            access_scope=backend_scope,
            project_root=loaded_agent_cfg.project_root,
        )
    extra = dict(getattr(loaded_agent_cfg, "extra", None) or {})
    return AgentConfig(
        name=loaded_agent_cfg.name,
        engine=engine,
        workspace_dir=loaded_agent_cfg.workspace_dir,
        system_md=loaded_agent_cfg.system_md,
        model=model,
        is_active=True,
        resume_policy=getattr(loaded_agent_cfg, "resume_policy", "latest"),
        extra=extra,
        access_scope=loaded_agent_cfg.access_scope,
        project_root=loaded_agent_cfg.project_root,
    )


async def _call_current_backend(prompt: str):
    backend, model = _resolve_backend_context()
    if backend not in SUPPORTED_SAFE_BACKENDS:
        raise RuntimeError(f"不允许的 backend: {backend}")
    global_cfg, loaded_agent_cfg, secrets = _load_runtime_config()
    adapter_cfg = _build_direct_adapter_config(backend, model, loaded_agent_cfg)
    backend_class = get_backend_class(backend)
    api_key = _resolve_api_key_direct(backend, secrets)
    adapter = backend_class(adapter_cfg, global_cfg, api_key)
    if not await adapter.initialize():
        raise RuntimeError(f"backend 初始化失败: {backend}")
    request_id = f"wiki-review-{int(time.time()*1000)}"
    try:
        response = await adapter.generate_response(prompt, request_id, silent=True)
    finally:
        await adapter.shutdown()
    if not response.is_success:
        raise RuntimeError(response.error or "backend 返回失败")
    return response.text, response


def _extract_json(text: str):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("模型输出中找不到 JSON")
    return json.loads(m.group(0))


def _truncate(text: str, limit: int = 220) -> str:
    compact = " ".join(str(text or "").split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def _memory_lines(memories: list[dict], limit: int) -> str:
    lines = []
    for idx, mem in enumerate(memories[:limit], start=1):
        lines.append(
            f"{idx}. [{mem.get('date','?')}] {mem.get('agent','?')}: {_truncate(mem.get('content',''), 260)}"
        )
    return "\n".join(lines)


def _date_range(memories: list[dict]) -> tuple[str, str]:
    dates = [m.get("date") for m in memories if m.get("date")]
    return (min(dates), max(dates)) if dates else ("?", "?")


def _agent_links(memories: list[dict], limit: int = 8) -> str:
    agents = sorted({m.get("agent", "?") for m in memories if m.get("agent")})[:limit]
    return " · ".join(f"[[Agents/{a}]]" for a in agents)


async def _build_topic_page(topic_id: str, obj: dict) -> tuple[str, dict]:
    memories = obj["memories"]
    start, end = _date_range(memories)
    meta = obj["meta"]
    prompt = f"""你在为 Obsidian wiki 生成一个 Topic 页面。
只输出一个 JSON 对象，不要加代码块，不要加解释。

目标 Topic:
- id: {topic_id}
- display: {meta['display']}
- desc: {meta['desc']}
- memory_count: {len(memories)}
- date_range: {start} -> {end}
- top_agents: {Counter(m.get('agent','?') for m in memories).most_common(5)}

记忆样本:
{_memory_lines(memories, 18)}

返回 JSON，键必须是:
overview: string
key_points: string[] (4-7条)
decisions: array of {{"date": "...", "decision": "...", "source": "..."}}
recent_updates: string[] (3-5条)
agent_roles: array of {{"agent":"...", "role":"..."}}
quality_note: string

要求:
- 只基于给定记忆，不要假装知道没有证据的事
- 中文为主，允许保留必要英文术语
- 如果主题实际发生偏移，要诚实指出，不要强行套旧主题
"""
    text, resp = await _call_current_backend(prompt)
    data = _extract_json(text)
    md = [
        "---",
        "type: topic",
        f"name: {topic_id}",
        f"display: {meta['display']}",
        f"memory_count: {len(memories)}",
        f"date_range: {start} → {end}",
        f"last_updated: {datetime.now().strftime('%Y-%m-%d')}",
        f"tags: [topic, {topic_id.lower().replace('_', '-')}]",
        "---",
        "",
        f"# {meta['display']}",
        "",
        f"> **范围:** {meta['desc']}  ",
        f"> **记忆条数:** {len(memories)} · **日期跨度:** {start} → {end}  ",
        f"> **涉及 agents:** {_agent_links(memories)}",
        "",
        "---",
        "",
        "## 主题概述",
        "",
        data["overview"].strip(),
        "",
        "## 核心知识点",
        "",
    ]
    md.extend(f"- **{_truncate(p.split('：')[0], 80)}**：{p.split('：',1)[1].strip()}" if "：" in p else f"- {p}" for p in data["key_points"])
    md.extend(["", "## 关键决策记录", "", "| 日期 | 决策内容 | 决策者/来源 |", "|------|---------|------------|"])
    for row in data["decisions"][:6]:
        md.append(f"| {row.get('date','?')} | {row.get('decision','').strip()} | {row.get('source','?').strip()} |")
    md.extend(["", "## 最近动态", ""])
    md.extend(f"- {item}" for item in data["recent_updates"][:5])
    md.extend(["", "## 相关 Agents", ""])
    for row in data["agent_roles"][:6]:
        md.append(f"- **{row.get('agent','?')}**：{row.get('role','').strip()}")
    md.extend(["", "---", f"_自动生成并复核 · {datetime.now().strftime('%Y-%m-%d')}_"])
    usage = resp.usage
    tokens = (usage.input_tokens + usage.output_tokens + usage.thinking_tokens) if usage else None
    return "\n".join(md) + "\n", {"token_total": tokens, "quality_note": data.get("quality_note", "")}


async def _build_project_page(project_id: str, obj: dict) -> tuple[str, dict]:
    memories = obj["memories"]
    start, end = _date_range(memories)
    meta = obj["meta"]
    prompt = f"""你在为 Obsidian wiki 生成一个 Project 页面。
只输出一个 JSON 对象，不要加代码块。

目标 Project:
- id: {project_id}
- display: {meta['display']}
- desc: {meta['desc']}
- status: {meta['status']}
- memory_count: {len(memories)}
- date_range: {start} -> {end}
- top_agents: {Counter(m.get('agent','?') for m in memories).most_common(6)}

记忆样本:
{_memory_lines(memories, 20)}

返回 JSON，键必须是:
overview: string
architecture: string[] (3-6)
milestones: string[] (3-6)
working: string[] (2-5)
in_progress: string[] (2-5)
known_issues: string[] (1-4)
agents: array of {{"agent":"...", "role":"..."}}
next_steps: string[] (2-5)
quality_note: string

要求:
- 只根据提供的记忆写
- 如果项目证据不足或记忆错配，要明确说
- 中文为主，术语可保留英文
"""
    text, resp = await _call_current_backend(prompt)
    data = _extract_json(text)
    md = [
        "---",
        "type: project",
        f"name: {project_id}",
        f"status: {meta['status']}",
        f"last_updated: {datetime.now().strftime('%Y-%m-%d')}",
        "tags: [project, " + meta["status"] + "]",
        "---",
        "",
        f"# {meta['display']}",
        "",
        f"**状态:** {'🟢 Active' if meta['status']=='active' else '🟡 Planned'} · **最后更新:** {datetime.now().strftime('%Y-%m-%d')}",
        "",
        "---",
        "",
        "## 项目概述",
        "",
        data["overview"].strip(),
        "",
        "## 技术架构",
        "",
    ]
    md.extend(f"- {item}" for item in data["architecture"][:6])
    md.extend(["", "## 里程碑 & 进展", ""])
    md.extend(f"- {item}" for item in data["milestones"][:6])
    md.extend(["", "## 当前状态", "", "**✅ Working**"])
    md.extend(f"- {item}" for item in data["working"][:5])
    md.extend(["", "**🔄 In Progress**"])
    md.extend(f"- {item}" for item in data["in_progress"][:5])
    if data["known_issues"]:
        md.extend(["", "**⚠️ Known Issues**"])
        md.extend(f"- {item}" for item in data["known_issues"][:4])
    md.extend(["", "## 参与 Agents", ""])
    for row in data["agents"][:6]:
        md.append(f"- **{row.get('agent','?')}**：{row.get('role','').strip()}")
    md.extend(["", "## 下一步", ""])
    md.extend(f"{idx}. {item}" for idx, item in enumerate(data["next_steps"][:5], start=1))
    md.extend(["", "---", f"_自动生成并复核 · {datetime.now().strftime('%Y-%m-%d')}_"])
    usage = resp.usage
    tokens = (usage.input_tokens + usage.output_tokens + usage.thinking_tokens) if usage else None
    return "\n".join(md) + "\n", {"token_total": tokens, "quality_note": data.get("quality_note", "")}


async def _build_weekly_page(obj: dict) -> tuple[str, dict]:
    memories = obj["memories"]
    week = obj["week_str"]
    start = obj["week_start"]
    end = obj["week_end"]
    prompt = f"""你在为 Obsidian wiki 生成每周 digest。
只输出一个 JSON 对象，不要加代码块。

周次: {week}
时间: {start} -> {end}
memory_count: {len(memories)}
top_agents: {Counter(m.get('agent','?') for m in memories).most_common(8)}

记忆样本:
{_memory_lines(memories, 28)}

返回 JSON，键必须是:
summary: string
themes: array of {{"title":"...", "points":["...", "..."]}}
decisions: string[]
highlights: string[]
open_issues: string[]
quality_note: string

要求:
- 只根据本周记忆归纳
- 聚焦真正的主线，不要泛泛而谈
- 中文为主
"""
    text, resp = await _call_current_backend(prompt)
    data = _extract_json(text)
    agents = sorted({m.get("agent", "?") for m in memories if m.get("agent")})
    md = [
        "---",
        "type: weekly",
        f"week: {week}",
        f"start: {start}",
        f"end: {end}",
        f"memory_count: {len(memories)}",
        "agents: [" + ", ".join(agents) + "]",
        f"last_updated: {datetime.now().strftime('%Y-%m-%d')}",
        "tags: [weekly, digest]",
        "---",
        "",
        f"# 每周消化 — {week}",
        "",
        f"**{start} — {end}** · {len(memories)} 条记忆",
        "",
        "**Active Agents:** " + " · ".join(f"[[Agents/{a}]]" for a in agents),
        "",
        "---",
        "",
        "## 本周摘要",
        "",
        data["summary"].strip(),
        "",
        "## 主要主题",
        "",
    ]
    for theme in data["themes"][:5]:
        md.append(f"### {theme.get('title','未命名主题')}")
        for point in theme.get("points", [])[:4]:
            md.append(f"- {point}")
        md.append("")
    md.extend(["## 重要决策", ""])
    md.extend(f"- {item}" for item in data["decisions"][:6])
    md.extend(["", "## 本周亮点", ""])
    md.extend(f"- {item}" for item in data["highlights"][:4])
    md.extend(["", "## 遗留问题 & 下周关注", ""])
    md.extend(f"- {item}" for item in data["open_issues"][:6])
    md.extend(["", "---", f"_自动生成并复核 · {datetime.now().strftime('%Y-%m-%d')}_"])
    usage = resp.usage
    tokens = (usage.input_tokens + usage.output_tokens + usage.thinking_tokens) if usage else None
    return "\n".join(md) + "\n", {"token_total": tokens, "quality_note": data.get("quality_note", "")}


def _write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


async def _review_pages(page_infos: list[dict]) -> tuple[dict, int | None]:
    summary_lines = []
    for info in page_infos:
        excerpt = _truncate(info["path"].read_text(encoding="utf-8"), 900)
        summary_lines.append(
            f"- page: {info['label']}\n  type: {info['kind']}\n  quality_note: {info['quality_note']}\n  excerpt: {excerpt}"
        )
    prompt = f"""你刚刚为 weekly wiki job 生成/更新了一批页面。现在请做一次严格复核。
只输出 JSON，不要加解释。

页面摘要:
{chr(10).join(summary_lines)}

返回 JSON，键必须是:
overall: string
ai_value: string
weak_pages: array of {{"page":"...", "issue":"...", "severity":"high|medium|low"}}
anomalies: string[]

要求:
- 不要粉饰太平
- 如果有页面只是机械整理、主题错配、结构重复或证据不足，要直接指出
"""
    text, resp = await _call_current_backend(prompt)
    data = _extract_json(text)
    usage = resp.usage
    tokens = (usage.input_tokens + usage.output_tokens + usage.thinking_tokens) if usage else None
    return data, tokens


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--daily-tags", type=int, default=0)
    args = parser.parse_args()

    manifest = _read_json(DUMP_DIR / "manifest.json", None)
    if not manifest:
        raise SystemExit("manifest.json 不存在，不能继续。")

    backend, model = _resolve_backend_context()
    today = datetime.now().strftime("%Y-%m-%d")
    token_totals = []
    pages: list[dict] = []
    topic_results = []
    project_results = []
    errors = []

    for topic_id in manifest["topics"]:
        try:
            obj = _read_json(DUMP_DIR / f"topic_{topic_id}.json", None)
            content, meta = await _build_topic_page(topic_id, obj)
            path = VAULT_ROOT / "Topics" / f"{topic_id}.md"
            _write(path, content)
            pages.append({"label": topic_id, "kind": "topic", "path": path, "quality_note": meta["quality_note"]})
            token_totals.append(meta["token_total"])
            topic_results.append((topic_id, path))
        except Exception as exc:
            errors.append(f"Topic {topic_id}: {exc}")

    for project_id in manifest["projects"]:
        try:
            obj = _read_json(DUMP_DIR / f"project_{project_id}.json", None)
            content, meta = await _build_project_page(project_id, obj)
            path = VAULT_ROOT / "Projects" / f"{project_id}.md"
            _write(path, content)
            pages.append({"label": project_id, "kind": "project", "path": path, "quality_note": meta["quality_note"]})
            token_totals.append(meta["token_total"])
            project_results.append((project_id, path))
        except Exception as exc:
            errors.append(f"Project {project_id}: {exc}")

    weekly_meta = None
    try:
        obj = _read_json(DUMP_DIR / f"weekly_{manifest['weekly']['week']}.json", None)
        content, meta = await _build_weekly_page(obj)
        weekly_path = VAULT_ROOT / "Weekly" / f"{manifest['weekly']['week']}.md"
        _write(weekly_path, content)
        pages.append({"label": manifest["weekly"]["week"], "kind": "weekly", "path": weekly_path, "quality_note": meta["quality_note"]})
        token_totals.append(meta["token_total"])
        weekly_meta = (manifest["weekly"]["week"], weekly_path)
    except Exception as exc:
        errors.append(f"Weekly {manifest['weekly']['week']}: {exc}")

    review_data, review_tokens = await _review_pages(pages)
    token_totals.append(review_tokens)

    known_tokens = [t for t in token_totals if isinstance(t, int)]
    total_tokens = sum(known_tokens) if known_tokens else None
    token_line = str(total_tokens) if total_tokens is not None else f"不可用（{backend} / {model} 为 CLI backend，脚本层未拿到稳定 token usage）"

    lines = [
        f"# Wiki 整理报告 — {today}",
        "",
        f"- backend: `{backend}` / `{model}`",
        f"- daily tags updated: `{args.daily_tags}`",
        f"- topics processed: `{len(topic_results)}` / `{len(manifest['topics'])}`",
        f"- projects processed: `{len(project_results)}` / `{len(manifest['projects'])}`",
        f"- weekly digest: `{weekly_meta[0]}`" if weekly_meta else "- weekly digest: `FAILED`",
        f"- total token usage: `{token_line}`",
        "",
        "## Topics",
        "",
    ]
    for topic_id, path in topic_results:
        lines.append(f"- `{topic_id}`: 成功 → {path}")
    lines.extend(["", "## Projects", ""])
    for project_id, path in project_results:
        lines.append(f"- `{project_id}`: 成功 → {path}")
    lines.extend(["", "## Weekly", ""])
    if weekly_meta:
        lines.append(f"- `{weekly_meta[0]}`: 成功 → {weekly_meta[1]}")
    else:
        lines.append("- Weekly digest 生成失败")
    lines.extend(["", "## AI Review", "", f"- overall: {review_data.get('overall','')}", f"- ai_value: {review_data.get('ai_value','')}"])
    weak_pages = review_data.get("weak_pages") or []
    if weak_pages:
        lines.extend(["", "### Weak Pages"])
        for row in weak_pages:
            lines.append(f"- `{row.get('page','?')}` [{row.get('severity','?')}]: {row.get('issue','')}")
    anomalies = review_data.get("anomalies") or []
    if anomalies:
        lines.extend(["", "### Anomalies"])
        for item in anomalies:
            lines.append(f"- {item}")
    if errors:
        lines.extend(["", "## Errors"])
        for err in errors:
            lines.append(f"- {err}")
    else:
        lines.extend(["", "## Errors", "", "- 无脚本错误；若有质量问题，已在 AI Review 中单列。"])

    report_text = "\n".join(lines) + "\n"
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORT_DIR / f"wiki_organise_report_{today}.md"
    _write(report_path, report_text)
    _write(LATEST_REPORT, report_text)
    print(report_text)

    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
