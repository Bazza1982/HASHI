#!/usr/bin/env python3
"""Dual-brain sidecar: left-brain preflight and after-action continuity updates."""

from __future__ import annotations

import argparse
import heapq
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except ModuleNotFoundError:  # Windows native runtime has no fcntl.
    fcntl = None

from dual_brain_common import (
    BackendContext,
    git_check_ignored,
    load_config,
    require_config,
    resolve_backend,
    write_json,
)


def _lock_file(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_file(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_jsonl(path: Path, max_lines: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    if max_lines > 0:
        rows = rows[-max_lines:]
    return rows


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        _lock_file(f)
        try:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        finally:
            _unlock_file(f)


def _read_bool(mapping: dict[str, Any], key: str, default: bool) -> bool:
    value = mapping.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"false", "no", "0", "off"}:
            return False
        if normalized in {"true", "yes", "1", "on"}:
            return True
    return bool(value)


def _call_backend(context: BackendContext, prompt: str, cwd: Path, timeout_s: int) -> str:
    if context.backend == "codex-cli":
        argv = [
            context.command,
            "exec",
            "--json",
            "--model",
            context.model,
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "-",
        ]
        result = subprocess.run(argv, input=prompt, text=True, capture_output=True, cwd=str(cwd), timeout=timeout_s)
        if result.returncode != 0:
            raise RuntimeError(f"codex-cli failed: {result.stderr.strip()}")
        latest = ""
        for line in (result.stdout or "").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item") or {}
            if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                latest = str(item.get("text") or "")
        if not latest:
            raise RuntimeError("codex-cli produced no agent_message")
        return latest

    if context.backend == "claude-cli":
        argv = [context.command, "--model", context.model, "--print"]
        result = subprocess.run(argv, input=prompt, text=True, capture_output=True, cwd=str(cwd), timeout=timeout_s)
        if result.returncode != 0:
            raise RuntimeError(f"claude-cli failed: {result.stderr.strip()}")
        return result.stdout or ""

    if context.backend == "gemini-cli":
        argv = [context.command, "--model", context.model, "--prompt", prompt]
        result = subprocess.run(argv, text=True, capture_output=True, cwd=str(cwd), timeout=timeout_s)
        if result.returncode != 0:
            raise RuntimeError(f"gemini-cli failed: {result.stderr.strip()}")
        return result.stdout or ""

    raise RuntimeError(f"unsupported backend: {context.backend}")


def _extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        raise RuntimeError("empty backend output")

    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for idx in range(start, len(text)):
            char = text[idx]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : idx + 1]
                    try:
                        obj = json.loads(candidate)
                    except json.JSONDecodeError:
                        break
                    if isinstance(obj, dict):
                        return obj
                    break
        start = text.find("{", start + 1)
    raise RuntimeError("no JSON object found in backend output")


def _wiki_candidates(wiki_root: Path, limit: int, *, query: str = "") -> list[dict[str, str]]:
    if not wiki_root.exists():
        return []
    query_terms = {
        term.lower()
        for term in re.findall(r"[\w\u4e00-\u9fff]{2,}", query)
        if len(term.strip()) >= 2
    }
    scored: list[tuple[int, float, Path, str]] = []
    for path in wiki_root.rglob("*.md"):
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        haystack = f"{path.stem}\n{content}".lower()
        score = sum(1 for term in query_terms if term in haystack)
        if query_terms and score <= 0:
            continue
        scored.append((score, path.stat().st_mtime, path, content))
    selected = heapq.nlargest(limit, scored, key=lambda item: (item[0], item[1]))
    out: list[dict[str, str]] = []
    for score, _mtime, path, content in selected:
        out.append({"path": str(path), "score": score, "snippet": content[:1200]})
    return out


def _resolve_wiki_roots(root: Path, cfg: dict[str, Any]) -> list[Path]:
    raw_roots = cfg.get("wiki_roots")
    if raw_roots is None:
        raw_roots = [require_config(cfg, "wiki_root")]
    if not isinstance(raw_roots, list) or not raw_roots:
        raise RuntimeError("config key wiki_roots must be a non-empty list")
    paths = []
    for raw in raw_roots:
        path = Path(str(raw)).expanduser()
        paths.append(path if path.is_absolute() else root / path)
    return paths


def _wiki_candidates_from_roots(wiki_roots: list[Path], limit: int, *, query: str = "") -> list[dict[str, str]]:
    query_terms = {
        term.lower()
        for term in re.findall(r"[\w\u4e00-\u9fff]{2,}", query)
        if len(term.strip()) >= 2
    }
    candidates: list[tuple[int, float, Path, str]] = []
    for wiki_root in wiki_roots:
        if not wiki_root.exists():
            continue
        for path in wiki_root.rglob("*.md"):
            try:
                content = path.read_text(encoding="utf-8")
            except Exception:
                continue
            haystack = f"{path.stem}\n{content}".lower()
            score = sum(1 for term in query_terms if term in haystack)
            if query_terms and score <= 0:
                continue
            candidates.append((score, path.stat().st_mtime, path, content))
    selected = heapq.nlargest(limit, candidates, key=lambda item: (item[0], item[1]))
    out: list[dict[str, str]] = []
    for score, _mtime, path, content in selected:
        out.append({"path": str(path), "score": score, "snippet": content[:1200]})
    return out


def cmd_diagnose(args: argparse.Namespace) -> int:
    root = Path(args.hashi_root).resolve()
    cfg_path = Path(args.config).resolve()
    cfg = load_config(cfg_path)

    print(f"[dual_brain] mode=diagnose")
    print(f"[dual_brain] hashi_root={root}")
    print(f"[dual_brain] config_path={cfg_path}")

    ctx = resolve_backend(root, args.agent, cfg, role="left_brain")
    print(f"[dual_brain] selected_backend={ctx.backend}")
    print(f"[dual_brain] selected_model={ctx.model}")
    print(f"[dual_brain] backend_command={ctx.command}")
    print(f"[dual_brain] backend_source={ctx.source}")

    agent = args.agent
    workspace = root / "workspaces" / agent
    continuity_file = workspace / str(require_config(cfg, "continuity_file"))
    out_dir = workspace / str(require_config(cfg, "output_dir"))
    wiki_roots = _resolve_wiki_roots(root, cfg)
    continuity_ignored, continuity_ignore_reason = git_check_ignored(root, continuity_file)
    out_ignored, out_ignore_reason = git_check_ignored(root, out_dir)

    print(f"[dual_brain] agent={agent}")
    print(f"[dual_brain] continuity_file={continuity_file}")
    print(f"[dual_brain] output_dir={out_dir}")
    print(f"[dual_brain] wiki_roots={','.join(str(path) for path in wiki_roots)}")
    print(f"[dual_brain] continuity_exists={continuity_file.exists()}")
    print(f"[dual_brain] wiki_roots_existing={sum(1 for path in wiki_roots if path.exists())}/{len(wiki_roots)}")
    print(f"[dual_brain] continuity_gitignored={continuity_ignored} ({continuity_ignore_reason})")
    print(f"[dual_brain] output_dir_gitignored={out_ignored} ({out_ignore_reason})")
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    root = Path(args.hashi_root).resolve()
    cfg_path = Path(args.config).resolve()
    cfg = load_config(cfg_path)

    print(f"[dual_brain] mode=preflight")
    print(f"[dual_brain] hashi_root={root}")
    print(f"[dual_brain] config_path={cfg_path}")

    ctx = resolve_backend(root, args.agent, cfg, role="left_brain")
    print(f"[dual_brain] selected_backend={ctx.backend}")
    print(f"[dual_brain] selected_model={ctx.model}")
    print(f"[dual_brain] backend_source={ctx.source}")

    agent = args.agent
    workspace = root / "workspaces" / agent
    continuity_file = workspace / str(require_config(cfg, "continuity_file"))
    out_dir = workspace / str(require_config(cfg, "output_dir"))
    wiki_roots = _resolve_wiki_roots(root, cfg)

    user_prompt = args.prompt
    if args.prompt_file:
        user_prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    if not user_prompt.strip():
        raise RuntimeError("empty user prompt")

    continuity = _read_jsonl(continuity_file, 0)

    print(f"[dual_brain] continuity_entries_loaded={len(continuity)}")
    print("[dual_brain] wiki_stage=not_requested_initially")

    schema = {
        "useful": True,
        "wiki_needed": False,
        "wiki_query": "",
        "same_day_context": ["..."],
        "open_items": ["..."],
        "notes_for_executor": ["..."],
        "sources": ["..."],
        "confidence": 0.0
    }

    llm_prompt = (
        "You are an LLM context and memory organiser for HASHI dual-brain mode. "
        f"You maintain the continuity notebook at {continuity_file} so same-day context "
        "and mid-term memory flow correctly to the next LLM, which will execute the user's "
        "original prompt. For each user prompt, first read the supplied continuity notebook "
        "contents and decide what context, if any, should be passed forward. Besides the continuity "
        "notebook, you may request HASHI wiki (/wiki) retrieval as long-term memory only when "
        "the notebook is insufficient for understanding or supporting the current user prompt. "
        "You do not execute the user's prompt, answer the user, rewrite the prompt, or plan "
        "the task for the execution model. You only provide relevant context that may help the "
        "next model perform the task. Return JSON only.\\n\\n"
        f"USER_PROMPT:\\n{user_prompt}\\n\\n"
        f"CONTINUITY_JSONL:\\n{json.dumps(continuity, ensure_ascii=False)}\\n\\n"
        "Return an object with this schema:\\n"
        f"{json.dumps(schema, ensure_ascii=False)}"
    )

    response = _call_backend(ctx, llm_prompt, root, int(cfg.get("llm_timeout_s", 120)))
    fyi = _extract_json_object(response)
    wiki_used = False
    wiki_query = str(fyi.get("wiki_query") or user_prompt).strip()
    wiki: list[dict[str, str]] = []

    if bool(fyi.get("wiki_needed")):
        wiki_used = True
        wiki = _wiki_candidates_from_roots(
            wiki_roots,
            int(cfg.get("wiki_candidate_limit", 12)),
            query=wiki_query,
        )
        print(f"[dual_brain] wiki_stage=requested query={wiki_query!r}")
        print(f"[dual_brain] wiki_candidates_loaded={len(wiki)}")
        wiki_schema = {
            "useful": True,
            "wiki_used": True,
            "same_day_context": ["..."],
            "wiki_context": ["..."],
            "open_items": ["..."],
            "notes_for_executor": ["..."],
            "sources": ["..."],
            "confidence": 0.0
        }
        wiki_prompt = (
            "You are an LLM context and memory organiser for HASHI dual-brain mode. "
            f"You maintain the continuity notebook at {continuity_file}. The first pass "
            "decided older long-term memory is needed. You now receive wiki retrieval "
            "candidates. They may be irrelevant. Select only concise FYI context that helps "
            "the execution model with the original user message. Return JSON only.\\n\\n"
            f"USER_PROMPT:\\n{user_prompt}\\n\\n"
            f"NOTEPAD_FIRST_PASS_JSON:\\n{json.dumps(fyi, ensure_ascii=False)}\\n\\n"
            f"CONTINUITY_JSONL:\\n{json.dumps(continuity, ensure_ascii=False)}\\n\\n"
            f"WIKI_QUERY:\\n{wiki_query}\\n\\n"
            f"WIKI_CANDIDATES:\\n{json.dumps(wiki, ensure_ascii=False)}\\n\\n"
            "Return an object with this schema:\\n"
            f"{json.dumps(wiki_schema, ensure_ascii=False)}"
        )
        wiki_response = _call_backend(ctx, wiki_prompt, root, int(cfg.get("llm_timeout_s", 120)))
        fyi = _extract_json_object(wiki_response)
    else:
        print("[dual_brain] wiki_stage=not_needed")

    fyi_doc = {
        "generated_at": _now_iso(),
        "stage": "preflight",
        "agent": agent,
        "backend": ctx.backend,
        "model": ctx.model,
        "original_prompt": user_prompt,
        "fyi": fyi,
        "meta": {
            "wiki_used": wiki_used,
            "wiki_query": wiki_query if wiki_used else "",
            "wiki_candidates_loaded": len(wiki),
        },
        "note": "FYI only. This does not override user prompt or higher-priority instructions.",
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "left_brain_preflight_latest.json", fyi_doc)
    _append_jsonl(out_dir / "left_brain_events.jsonl", fyi_doc)

    md = [
        "<left_brain_fyi>",
        "This FYI does not modify or override the user's prompt.",
        "",
        "## Original Prompt",
        user_prompt,
        "",
        "## FYI JSON",
        "```json",
        json.dumps(fyi, ensure_ascii=False, indent=2),
        "```",
        "",
        "</left_brain_fyi>",
    ]
    (out_dir / "left_brain_fyi_latest.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    print(f"[dual_brain] success=true")
    print(f"[dual_brain] preflight_json={out_dir / 'left_brain_preflight_latest.json'}")
    print(f"[dual_brain] fyi_markdown={out_dir / 'left_brain_fyi_latest.md'}")
    return 0


def cmd_after_action(args: argparse.Namespace) -> int:
    root = Path(args.hashi_root).resolve()
    cfg_path = Path(args.config).resolve()
    cfg = load_config(cfg_path)

    print(f"[dual_brain] mode=after-action")
    print(f"[dual_brain] hashi_root={root}")
    print(f"[dual_brain] config_path={cfg_path}")

    ctx = resolve_backend(root, args.agent, cfg, role="left_brain")
    print(f"[dual_brain] selected_backend={ctx.backend}")
    print(f"[dual_brain] selected_model={ctx.model}")
    print(f"[dual_brain] backend_source={ctx.source}")

    agent = args.agent
    workspace = root / "workspaces" / agent
    continuity_file = workspace / str(require_config(cfg, "continuity_file"))
    out_dir = workspace / str(require_config(cfg, "output_dir"))

    user_prompt = args.prompt
    if args.prompt_file:
        user_prompt = Path(args.prompt_file).read_text(encoding="utf-8")

    result_text = args.result
    if args.result_file:
        result_text = Path(args.result_file).read_text(encoding="utf-8")
    if not result_text.strip():
        raise RuntimeError("empty execution result")
    result_max_chars = int(cfg.get("after_action_result_max_chars", 8000))
    result_truncated = len(result_text) > result_max_chars
    result_for_llm = result_text[:result_max_chars]
    print(
        "[dual_brain] after_action_result_truncated="
        f"{str(result_truncated).lower()} max_chars={result_max_chars}"
    )

    continuity = _read_jsonl(continuity_file, 0)
    print(f"[dual_brain] continuity_entries_loaded={len(continuity)}")

    schema = {
        "should_write": True,
        "continuity_summary": "",
        "decisions": ["..."],
        "commitments": ["..."],
        "state_changes": ["..."],
        "open_items": ["..."],
        "expiry_hints": [],
        "confidence": 0.0,
    }

    llm_prompt = (
        "You are an LLM continuity notebook updater for HASHI dual-brain mode. "
        f"You maintain the continuity notebook at {continuity_file}. The continuity notebook "
        "is a same-day and mid-term memory artefact. It exists to preserve useful continuity "
        "for future turns, not to summarize every response. After the execution model finishes "
        "answering the user's original prompt, read the user's original prompt, the execution "
        "model's final result, and the current continuity notebook contents. Decide what, if anything, "
        "should be written into the continuity notebook. Only record information that may matter "
        "in future turns, such as user decisions or preferences, commitments made by the assistant, "
        "changed project/file/system state, unresolved follow-up tasks, important context that "
        "would be costly to rediscover, or corrections to previous assumptions. Do not record "
        "routine chat, generic explanations, temporary wording, or a full summary of the answer. "
        "Return JSON only.\\n\\n"
        f"USER_PROMPT:\\n{user_prompt}\\n\\n"
        f"RIGHT_BRAIN_RESULT:\\n{result_for_llm}\\n\\n"
        f"CONTINUITY_JSONL:\\n{json.dumps(continuity, ensure_ascii=False)}\\n\\n"
        "Return this schema:\\n"
        f"{json.dumps(schema, ensure_ascii=False)}"
    )

    response = _call_backend(ctx, llm_prompt, root, int(cfg.get("llm_timeout_s", 120)))
    update = _extract_json_object(response)
    should_write = _read_bool(update, "should_write", True)

    row = {
        "ts": _now_iso(),
        "stage": "after_action",
        "agent": agent,
        "prompt": user_prompt,
        "right_brain_result_excerpt": result_text[:2000],
        "right_brain_result_truncated_for_llm": result_truncated,
        "right_brain_result_llm_chars": len(result_for_llm),
        "continuity_update": update,
        "written_to_continuity": should_write,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "left_brain_after_action_latest.json", row)
    _append_jsonl(out_dir / "left_brain_events.jsonl", row)
    if should_write:
        _append_jsonl(continuity_file, row)

    print(f"[dual_brain] success=true")
    print(f"[dual_brain] continuity_written={str(should_write).lower()}")
    print(f"[dual_brain] continuity_file={continuity_file}")
    print(f"[dual_brain] after_action_json={out_dir / 'left_brain_after_action_latest.json'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dual-brain left-brain sidecar CLI")
    parser.add_argument("--hashi-root", default="/home/lily/projects/hashi")
    parser.add_argument("--config", default="/home/lily/projects/hashi/private/dual_brain_config.json")
    parser.add_argument("--agent", default="lily")

    sub = parser.add_subparsers(dest="cmd", required=True)

    diag = sub.add_parser("diagnose", help="Print resolved paths/backend and safety checks")
    diag.set_defaults(func=cmd_diagnose)

    pre = sub.add_parser("preflight", help="Generate left-brain FYI context packet")
    pre.add_argument("--prompt", default="")
    pre.add_argument("--prompt-file", default="")
    pre.set_defaults(func=cmd_preflight)

    aft = sub.add_parser("after-action", help="Write continuity update after right-brain execution")
    aft.add_argument("--prompt", default="")
    aft.add_argument("--prompt-file", default="")
    aft.add_argument("--result", default="")
    aft.add_argument("--result-file", default="")
    aft.set_defaults(func=cmd_after_action)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"[dual_brain] success=false", file=sys.stderr)
        print(f"[dual_brain] error={exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
