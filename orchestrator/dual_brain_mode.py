from __future__ import annotations

import asyncio
import fcntl
import heapq
import json
import logging
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from orchestrator.post_turn_observer import (
    PostTurnObserver,
    PreTurnContextProvider,
    TurnContextRequest,
    TurnObservationRequest,
)


DUAL_BRAIN_OBSERVER_FACTORY = "orchestrator.dual_brain_mode:build_dual_brain_observer"
LEGACY_DEFAULT_LEFT_PROMPT = (
    "You are HASHI's left brain. You handle past-facing continuity and memory. "
    "Output JSON only. Do not execute the user's task. Do not rewrite user intent. "
    "Build sufficient FYI context for the right brain."
)
LEGACY_DEFAULT_LEFT_PROMPT_V2 = (
    "Role: memory briefing generator for HASHI dual-brain mode. "
    "First read only the user's original message and the same-day continuity notes. "
    "Return JSON only. Decide whether same-day memory is sufficient. Request older wiki "
    "memory only when the notepad is insufficient for the current message. Do not answer "
    "the user, plan the task, rewrite the user's message, or add execution instructions."
)
LEGACY_DEFAULT_AFTER_ACTION_PROMPT = (
    "You are HASHI's left brain continuity clerk. Output JSON only. "
    "Summarize only continuity-relevant facts for next turns."
)
LEGACY_DEFAULT_AFTER_ACTION_PROMPT_V2 = (
    "Role: continuity note updater for HASHI dual-brain mode. "
    "Task: read the user's message and the execution result, then return JSON only with "
    "facts worth keeping for later turns today. Keep only decisions, commitments, changed "
    "state, useful preferences, and unresolved follow-ups. Ignore routine chatter and do "
    "not summarize the whole answer."
)
DEFAULT_LEFT_PROMPT = (
    "You are an LLM context and memory organiser for HASHI dual-brain mode. "
    "You maintain the continuity notebook at workspaces/<agent>/memory/left_brain_continuity.jsonl "
    "so same-day context and mid-term memory flow correctly to the next LLM, which will execute "
    "the user's original prompt. For each user prompt, first read the supplied continuity notebook "
    "contents and decide what context, if any, should be passed forward. Besides the continuity notebook, "
    "you may request HASHI wiki (/wiki) retrieval as long-term memory only when the notebook is "
    "insufficient for understanding or supporting the current user prompt. You do not execute the "
    "user's prompt, answer the user, rewrite the prompt, or plan the task for the execution model. "
    "You only provide relevant context that may help the next model perform the task. Return JSON only."
)
DEFAULT_AFTER_ACTION_PROMPT = (
    "You are an LLM continuity notebook updater for HASHI dual-brain mode. "
    "You maintain the continuity notebook at workspaces/<agent>/memory/left_brain_continuity.jsonl. "
    "The continuity notebook is a same-day and mid-term memory artefact. It exists to preserve "
    "useful continuity for future turns, not to summarize every response. After the execution "
    "model finishes answering the user's original prompt, read the user's original prompt, the "
    "execution model's final result, and the current continuity notebook contents. Decide what, if "
    "anything, should be written into the continuity notebook. Only record information that may "
    "matter in future turns, such as user decisions or preferences, commitments made by the "
    "assistant, changed project/file/system state, unresolved follow-up tasks, important context "
    "that would be costly to rediscover, or corrections to previous assumptions. Do not record "
    "routine chat, generic explanations, temporary wording, or a full summary of the answer. "
    "Return JSON only."
)


@dataclass(frozen=True)
class DualBrainConfig:
    left_backend: str
    left_model: str
    right_backend: str
    right_model: str
    left_prompt: str
    after_action_prompt: str
    wiki_candidate_limit: int = 12
    after_action_result_max_chars: int = 8000


def load_dual_brain_config(state: Mapping[str, Any] | None, *, current_backend: str = "", current_model: str = "") -> DualBrainConfig:
    state_map = state if isinstance(state, Mapping) else {}
    block = state_map.get("dual_brain")
    cfg = block if isinstance(block, Mapping) else {}
    left = cfg.get("left_brain") if isinstance(cfg.get("left_brain"), Mapping) else {}
    right = cfg.get("right_brain") if isinstance(cfg.get("right_brain"), Mapping) else {}
    prompts = cfg.get("prompts") if isinstance(cfg.get("prompts"), Mapping) else {}
    left_prompt = _read_str(prompts, "left", DEFAULT_LEFT_PROMPT)
    if left_prompt in {LEGACY_DEFAULT_LEFT_PROMPT, LEGACY_DEFAULT_LEFT_PROMPT_V2}:
        left_prompt = DEFAULT_LEFT_PROMPT
    after_action_prompt = _read_str(prompts, "after_action", DEFAULT_AFTER_ACTION_PROMPT)
    if after_action_prompt in {LEGACY_DEFAULT_AFTER_ACTION_PROMPT, LEGACY_DEFAULT_AFTER_ACTION_PROMPT_V2}:
        after_action_prompt = DEFAULT_AFTER_ACTION_PROMPT
    return DualBrainConfig(
        left_backend=_read_str(left, "backend", current_backend),
        left_model=_read_str(left, "model", current_model),
        right_backend=_read_str(right, "backend", current_backend),
        right_model=_read_str(right, "model", current_model),
        left_prompt=left_prompt,
        after_action_prompt=after_action_prompt,
        wiki_candidate_limit=_read_int(cfg, "wiki_candidate_limit", 12),
        after_action_result_max_chars=_read_int(cfg, "after_action_result_max_chars", 8000),
    )


def dual_brain_block_with(
    cfg: DualBrainConfig,
    *,
    left_backend: str | None = None,
    left_model: str | None = None,
    right_backend: str | None = None,
    right_model: str | None = None,
    left_prompt: str | None = None,
    after_action_prompt: str | None = None,
) -> dict[str, Any]:
    return {
        "left_brain": {
            "backend": left_backend if left_backend is not None else cfg.left_backend,
            "model": left_model if left_model is not None else cfg.left_model,
        },
        "right_brain": {
            "backend": right_backend if right_backend is not None else cfg.right_backend,
            "model": right_model if right_model is not None else cfg.right_model,
        },
        "prompts": {
            "left": left_prompt if left_prompt is not None else cfg.left_prompt,
            "after_action": after_action_prompt if after_action_prompt is not None else cfg.after_action_prompt,
        },
        "wiki_candidate_limit": cfg.wiki_candidate_limit,
        "after_action_result_max_chars": cfg.after_action_result_max_chars,
    }


class DualBrainObserver(PostTurnObserver, PreTurnContextProvider):
    BYPASS_SOURCES = {
        "startup",
        "system",
        "scheduler",
        "scheduler-retry",
        "scheduler-skill",
        "loop_skill",
        "retry",
        "session_reset",
    }
    BYPASS_PREFIXES = (
        "bridge:",
        "bridge-transfer:",
        "hchat-reply:",
        "ticket:",
        "cos-query:",
    )

    def __init__(
        self,
        *,
        workspace_dir: Path,
        backend_invoker: Any,
        backend_context_getter: Any,
        options: dict[str, Any] | None = None,
    ):
        self.workspace_dir = workspace_dir
        self.backend_invoker = backend_invoker
        self.backend_context_getter = backend_context_getter
        self.options = options or {}
        self.logger = logging.getLogger("DualBrain.Observer")
        self.continuity_file = workspace_dir / "memory" / "left_brain_continuity.jsonl"
        self.artifacts_dir = workspace_dir / "memory" / "left_brain_artifacts"
        self.runtime: Any | None = None

    def attach_runtime(self, runtime: Any) -> None:
        self.runtime = runtime

    def should_provide(self, source: str, *, is_bridge_request: bool) -> bool:
        return self._enabled() and not self._should_bypass_source(source, is_bridge_request=is_bridge_request)

    def should_observe(self, source: str, *, is_bridge_request: bool) -> bool:
        return self._enabled() and not self._should_bypass_source(source, is_bridge_request=is_bridge_request)

    def on_right_brain_started(self, request: TurnObservationRequest) -> None:
        pending = {
            "ts": _now_iso(),
            "stage": "right_brain_started",
            "agent": self.workspace_dir.name,
            "request_id": request.request_id,
            "source": request.source,
            "summary": request.summary,
            "chat_id": request.chat_id,
            "prompt": request.user_text,
            "model_name": request.model_name,
            "final_prompt_chars": len(str(request.metadata.get("final_prompt") or "")),
        }
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        _write_json(self._pending_turn_path(request.request_id), pending)
        _write_json(self.artifacts_dir / "left_brain_right_brain_pending_latest.json", pending)
        _append_jsonl(self.artifacts_dir / "left_brain_events.jsonl", pending)

    def on_right_brain_completed(self, request: TurnObservationRequest) -> None:
        pending_path = self._pending_turn_path(request.request_id)
        pending = _read_json_object(pending_path)
        if pending_path.exists():
            pending_path.unlink(missing_ok=True)
        row = {
            "ts": _now_iso(),
            "stage": "right_brain_completed",
            "agent": self.workspace_dir.name,
            "request_id": request.request_id,
            "source": request.source,
            "summary": request.summary,
            "completion_path": request.metadata.get("completion_path", ""),
            "right_brain_result_chars": len(request.assistant_text or ""),
            "pending_started_at": pending.get("ts", ""),
        }
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        _write_json(self.artifacts_dir / "left_brain_right_brain_completed_latest.json", row)
        _append_jsonl(self.artifacts_dir / "left_brain_events.jsonl", row)

    def on_right_brain_interrupted(self, request: TurnObservationRequest) -> None:
        pending_path = self._pending_turn_path(request.request_id)
        if not pending_path.exists():
            return
        pending = _read_json_object(pending_path)
        pending_path.unlink(missing_ok=True)
        reason = str(request.metadata.get("reason") or "interrupted")
        error = str(request.metadata.get("error") or "")
        continuity_update = {
            "should_write": True,
            "continuity_summary": (
                "The right-brain execution turn did not complete normally. "
                "Preserve this interruption as continuity for the next turn."
            ),
            "decisions": [],
            "commitments": [],
            "state_changes": [
                f"Right brain interrupted for request {request.request_id} with reason={reason}."
            ],
            "open_items": [
                "Next turn may need to recover, retry, or ask the user whether to continue the interrupted task."
            ],
            "interruption": {
                "reason": reason,
                "error": error,
                "pending_started_at": pending.get("ts", ""),
            },
            "confidence": 1.0,
        }
        row = {
            "ts": _now_iso(),
            "agent": self.workspace_dir.name,
            "request_id": request.request_id,
            "prompt": request.user_text or pending.get("prompt", ""),
            "right_brain_result_excerpt": "",
            "right_brain_result_truncated_for_llm": False,
            "right_brain_result_llm_chars": 0,
            "continuity_update": continuity_update,
            "written_to_continuity": True,
            "interrupted_turn": True,
            "interruption_reason": reason,
            "interruption_error": error,
            "source": request.source,
            "summary": request.summary,
        }
        event = {**row, "stage": "interrupted_turn"}
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        _write_json(self.artifacts_dir / "left_brain_interrupted_turn_latest.json", row)
        _append_jsonl(self.artifacts_dir / "left_brain_events.jsonl", event)
        _append_jsonl(self.continuity_file, row)

    async def build_context_sections(self, request: TurnContextRequest) -> list[tuple[str, str]]:
        cfg = self._config()
        if not cfg.left_backend or not cfg.left_model:
            return []
        continuity = _read_jsonl(self.continuity_file, 0)
        left_prompt = cfg.left_prompt.replace(
            "workspaces/<agent>/memory/left_brain_continuity.jsonl",
            str(self.continuity_file),
        )
        schema = {
            "useful": True,
            "wiki_needed": False,
            "wiki_query": "",
            "same_day_context": ["..."],
            "open_items": ["..."],
            "notes_for_executor": ["..."],
            "sources": ["..."],
            "confidence": 0.0,
        }
        prompt = (
            f"{left_prompt}\n\n"
            "Inputs provided in this first pass:\n"
            "- USER_PROMPT: the user's original message.\n"
            "- CONTINUITY_JSONL: all same-day notepad entries.\n\n"
            "Do not request wiki unless older long-term memory is actually needed.\n\n"
            f"USER_PROMPT:\n{request.user_text}\n\n"
            f"CONTINUITY_JSONL:\n{json.dumps(continuity, ensure_ascii=False)}\n\n"
            "Return an object with this schema:\n"
            f"{json.dumps(schema, ensure_ascii=False)}"
        )
        response = await self.backend_invoker(
            engine=cfg.left_backend,
            model=cfg.left_model,
            prompt=prompt,
            request_id=f"{request.request_id}:left-brain",
            silent=True,
        )
        if not getattr(response, "is_success", False):
            self.logger.warning("Left-brain preflight failed for %s: %s", request.request_id, getattr(response, "error", ""))
            return []
        fyi = _extract_json_object(getattr(response, "text", "") or "")
        wiki_used = False
        wiki_query = str(fyi.get("wiki_query") or request.user_text).strip()
        wiki_candidates: list[dict[str, str]] = []

        if bool(fyi.get("wiki_needed")):
            wiki_used = True
            wiki_candidates = _wiki_candidates(self._wiki_roots(), cfg.wiki_candidate_limit, query=wiki_query)
            wiki_schema = {
                "useful": True,
                "wiki_used": True,
                "same_day_context": ["..."],
                "wiki_context": ["..."],
                "open_items": ["..."],
                "notes_for_executor": ["..."],
                "sources": ["..."],
                "confidence": 0.0,
            }
            wiki_prompt = (
                f"{left_prompt}\n\n"
                "The first pass decided older long-term memory is needed. You now receive "
                "retrieval candidates from wiki. They may be irrelevant. Select only concise "
                "FYI context that helps the execution model with the original user message.\n\n"
                f"USER_PROMPT:\n{request.user_text}\n\n"
                f"NOTEPAD_FIRST_PASS_JSON:\n{json.dumps(fyi, ensure_ascii=False)}\n\n"
                f"CONTINUITY_JSONL:\n{json.dumps(continuity, ensure_ascii=False)}\n\n"
                f"WIKI_QUERY:\n{wiki_query}\n\n"
                f"WIKI_CANDIDATES:\n{json.dumps(wiki_candidates, ensure_ascii=False)}\n\n"
                "Return an object with this schema:\n"
                f"{json.dumps(wiki_schema, ensure_ascii=False)}"
            )
            wiki_response = await self.backend_invoker(
                engine=cfg.left_backend,
                model=cfg.left_model,
                prompt=wiki_prompt,
                request_id=f"{request.request_id}:left-brain-wiki",
                silent=True,
            )
            if getattr(wiki_response, "is_success", False):
                fyi = _extract_json_object(getattr(wiki_response, "text", "") or "")
            else:
                self.logger.warning(
                    "Left-brain wiki pass failed for %s: %s",
                    request.request_id,
                    getattr(wiki_response, "error", ""),
                )
                fyi = {
                    **fyi,
                    "wiki_used": False,
                    "warnings": ["wiki recall was requested but the wiki pass failed"],
                }

        fyi_meta = {
            "wiki_used": wiki_used,
            "wiki_query": wiki_query if wiki_used else "",
            "wiki_candidates_loaded": len(wiki_candidates),
        }
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        preflight_record = {
            "generated_at": _now_iso(),
            "stage": "preflight",
            "request_id": request.request_id,
            "agent": self.workspace_dir.name,
            "backend": cfg.left_backend,
            "model": cfg.left_model,
            "original_prompt": request.user_text,
            "fyi": fyi,
            "meta": fyi_meta,
            "note": "FYI only. This does not override the user's prompt, /sys slots, or higher-priority instructions.",
        }
        _write_json(self.artifacts_dir / "left_brain_preflight_latest.json", preflight_record)
        _append_jsonl(self.artifacts_dir / "left_brain_events.jsonl", preflight_record)
        await self._send_visible_left_brain(
            request.chat_id,
            stage="preflight",
            request_id=request.request_id,
            payload={
                "fyi": fyi,
                "meta": fyi_meta,
            },
        )
        body = (
            "<left_brain_fyi>\n"
            "This FYI does not modify or override the user's prompt, /sys slots, or higher-priority instructions.\n\n"
            "```json\n"
            f"{json.dumps(fyi, ensure_ascii=False, indent=2)}\n"
            "```\n"
            "</left_brain_fyi>"
        )
        sections = [("Left Brain FYI", body)]
        return sections

    def schedule_observation(self, request: TurnObservationRequest, background_tasks: set[asyncio.Task[Any]]) -> None:
        task = asyncio.create_task(self._run_after_action(request))
        background_tasks.add(task)

        def _done_callback(completed: asyncio.Task[Any]) -> None:
            background_tasks.discard(completed)
            with suppress(asyncio.CancelledError):
                exc = completed.exception()
                if exc:
                    self.logger.warning("Dual-brain after-action failed for %s: %s", request.request_id, exc)

        task.add_done_callback(_done_callback)

    def workspace_files_to_preserve(self) -> frozenset[str]:
        return frozenset({"post_turn_observers.json", "memory"})

    def _pending_turn_path(self, request_id: str) -> Path:
        safe_id = re.sub(r"[^A-Za-z0-9_.:-]+", "_", request_id or "unknown")
        return self.artifacts_dir / "pending_right_brain" / f"{safe_id}.json"

    async def _run_after_action(self, request: TurnObservationRequest) -> None:
        cfg = self._config()
        if not cfg.left_backend or not cfg.left_model:
            return
        result_text = request.assistant_text or ""
        result_for_llm = result_text[: cfg.after_action_result_max_chars]
        continuity = _read_jsonl(self.continuity_file, 0)
        after_action_prompt = cfg.after_action_prompt.replace(
            "workspaces/<agent>/memory/left_brain_continuity.jsonl",
            str(self.continuity_file),
        )
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
        prompt = (
            f"{after_action_prompt}\n\n"
            f"USER_PROMPT:\n{request.user_text}\n\n"
            f"RIGHT_BRAIN_RESULT:\n{result_for_llm}\n\n"
            f"CONTINUITY_JSONL:\n{json.dumps(continuity, ensure_ascii=False)}\n\n"
            "Return this schema:\n"
            f"{json.dumps(schema, ensure_ascii=False)}"
        )
        response = await self.backend_invoker(
            engine=cfg.left_backend,
            model=cfg.left_model,
            prompt=prompt,
            request_id=f"{request.request_id}:left-brain-after",
            silent=True,
        )
        if not getattr(response, "is_success", False):
            raise RuntimeError(getattr(response, "error", "") or "left-brain after-action returned failure")
        update = _extract_json_object(getattr(response, "text", "") or "")
        should_write = _read_bool(update, "should_write", True)
        row = {
            "ts": _now_iso(),
            "agent": self.workspace_dir.name,
            "request_id": request.request_id,
            "prompt": request.user_text,
            "right_brain_result_excerpt": result_text[:2000],
            "right_brain_result_truncated_for_llm": len(result_text) > len(result_for_llm),
            "right_brain_result_llm_chars": len(result_for_llm),
            "continuity_update": update,
            "written_to_continuity": should_write,
        }
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        _write_json(self.artifacts_dir / "left_brain_after_action_latest.json", row)
        _append_jsonl(self.artifacts_dir / "left_brain_events.jsonl", {**row, "stage": "after_action"})
        await self._send_visible_left_brain(
            request.chat_id,
            stage="after_action",
            request_id=request.request_id,
            payload={
                "written_to_continuity": should_write,
                "continuity_update": update,
                "right_brain_result_truncated_for_llm": row["right_brain_result_truncated_for_llm"],
            },
        )
        if should_write:
            _append_jsonl(self.continuity_file, row)

    async def _send_visible_left_brain(
        self,
        chat_id: int | None,
        *,
        stage: str,
        request_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        runtime = self.runtime
        if runtime is None or chat_id is None:
            return
        if not (bool(getattr(runtime, "_verbose", False)) or bool(getattr(runtime, "_think", False))):
            return
        text = (
            f"💭 **Left brain {stage}** `{request_id}`\n\n"
            "```json\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
            "```"
        )
        with suppress(Exception):
            handoff_builder = getattr(runtime, "handoff_builder", None)
            if handoff_builder is not None:
                handoff_builder.append_transcript("thinking", text, "think")
        try:
            send_long = getattr(runtime, "send_long_message", None)
            if callable(send_long):
                await send_long(
                    chat_id=chat_id,
                    text=text,
                    request_id=f"{request_id}:left-brain-visible:{stage}",
                    purpose="left-brain-visible",
                )
                return
            send_text = getattr(runtime, "_send_text", None)
            if callable(send_text):
                await send_text(chat_id, text)
        except Exception as exc:
            self.logger.warning("Failed to send visible left-brain %s for %s: %s", stage, request_id, exc)

    def _config(self) -> DualBrainConfig:
        state = _read_json_object(self.workspace_dir / "state.json")
        current = self.backend_context_getter() if callable(self.backend_context_getter) else None
        current = current if isinstance(current, Mapping) else {}
        return load_dual_brain_config(
            state,
            current_backend=str(current.get("engine") or ""),
            current_model=str(current.get("model") or ""),
        )

    def _enabled(self) -> bool:
        state = _read_json_object(self.workspace_dir / "state.json")
        return str(state.get("agent_mode") or "").strip() == "dual-brain"

    def _wiki_roots(self) -> list[Path]:
        configured = self.options.get("wiki_roots")
        if isinstance(configured, list) and configured:
            return [Path(str(item)).expanduser() for item in configured]
        return [
            Path("/mnt/c/Users/thene/Documents/lily_hashi_wiki/10_GENERATED_TOPICS"),
            Path("/mnt/c/Users/thene/Documents/lily_hashi_wiki/30_GENERATED_INDEXES"),
        ]

    @classmethod
    def _should_bypass_source(cls, source: str, *, is_bridge_request: bool) -> bool:
        if is_bridge_request:
            return True
        normalized = (source or "").strip().lower()
        return normalized in cls.BYPASS_SOURCES or normalized.startswith(cls.BYPASS_PREFIXES)


def build_dual_brain_observer(
    *,
    workspace_dir: Path,
    bridge_memory_store: Any,
    backend_invoker: Any | None = None,
    backend_context_getter: Any | None = None,
    options: dict[str, Any] | None = None,
) -> DualBrainObserver | None:
    if backend_invoker is None or backend_context_getter is None:
        return None
    return DualBrainObserver(
        workspace_dir=workspace_dir,
        backend_invoker=backend_invoker,
        backend_context_getter=backend_context_getter,
        options=options,
    )


def ensure_dual_brain_observer(workspace_dir: Path) -> bool:
    path = workspace_dir / "post_turn_observers.json"
    config = _read_json_object(path)
    raw_observers = config.get("observers", [])
    observers = raw_observers if isinstance(raw_observers, list) else []
    changed = raw_observers is not observers
    found = False
    normalized: list[Any] = []
    for item in observers:
        if isinstance(item, str):
            if item == DUAL_BRAIN_OBSERVER_FACTORY:
                found = True
            normalized.append(item)
            continue
        if isinstance(item, dict):
            copied = dict(item)
            if str(copied.get("factory") or "").strip() == DUAL_BRAIN_OBSERVER_FACTORY:
                found = True
                if copied.get("enabled") is False:
                    copied["enabled"] = True
                    changed = True
            normalized.append(copied)
    if not found:
        normalized.append({"factory": DUAL_BRAIN_OBSERVER_FACTORY, "enabled": True})
        changed = True
    config["observers"] = normalized
    if changed or not path.exists():
        _write_json(path, config)
    return changed


def _read_str(mapping: Mapping[str, Any], key: str, default: str) -> str:
    value = mapping.get(key)
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _read_int(mapping: Mapping[str, Any], key: str, default: int) -> int:
    try:
        return max(1, int(mapping.get(key, default)))
    except (TypeError, ValueError):
        return default


def _read_bool(mapping: Mapping[str, Any], key: str, default: bool) -> bool:
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_jsonl(path: Path, max_lines: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows[-max_lines:] if max_lines > 0 else rows


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _wiki_candidates(wiki_roots: list[Path], limit: int, *, query: str = "") -> list[dict[str, str]]:
    query_terms = {
        term.lower()
        for term in re.findall(r"[\w\u4e00-\u9fff]{2,}", query)
        if len(term.strip()) >= 2
    }
    candidates: list[tuple[int, float, Path, str]] = []
    for root in wiki_roots:
        if not root.exists():
            continue
        for path in root.rglob("*.md"):
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


def _extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            return loaded
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
                        loaded = json.loads(candidate)
                    except json.JSONDecodeError:
                        break
                    if isinstance(loaded, dict):
                        return loaded
                    break
        start = text.find("{", start + 1)
    raise RuntimeError("no JSON object found in backend output")
