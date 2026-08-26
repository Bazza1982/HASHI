from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any

from orchestrator.runtime_retry import RETRY_HANDOFF_SOURCE

logger = logging.getLogger("BridgeU.HandoffBuilder")

class HandoffBuilder:
    EXCLUDED_RECENT_SOURCES = {
        "startup",
        "system",
        "think",
        "handoff",
        RETRY_HANDOFF_SOURCE,
    }
    EXCLUDED_TEXT_SNIPPETS = (
        "This is a fresh ",
        "Use those files as your operating context",
        "Behavior file loaded and confirmed.",
        "Working in `",
        "Send the task when you're ready.",
        "Send the task you want handled.",
        "Ready. Send the bridge-managed context",
        "No `NEW REQUEST` was included.",
        "Still no `NEW REQUEST`",
        "No `CURRENT USER REQUEST` was included.",
        "Still no `CURRENT USER REQUEST`",
        "Understood. I’ll treat `HANDOFF SUMMARY`",
        "Understood. I’ll use that material only as background",
        "Session instructions in effect:",
        "**Handoff Summary**",
    )

    def __init__(
        self,
        workspace_dir: Path,
        transcript_filename: str = "transcript.jsonl",
        *,
        canonical_audit: Any = None,
    ):
        self.workspace_dir = workspace_dir
        self.transcript_path = workspace_dir / transcript_filename
        self.recent_context_path = workspace_dir / "recent_context.jsonl"
        self.handoff_path = workspace_dir / "handoff.md"
        self.memory_dir = workspace_dir / "memory"
        
        self.max_recent_rounds = 15
        self.last_omission_audit: dict[str, Any] = {}
        self.canonical_audit = canonical_audit

    def append_transcript(self, role: str, text: str, source: str = "text"):
        entry = {
            "role": role,
            "text": text,
            "source": source,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        try:
            with open(self.transcript_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.error(f"Failed to append to transcript: {e}")

    def _load_rounds(self) -> List[List[Dict[str, Any]]]:
        if not self.transcript_path.exists():
            return []

        rounds: List[List[Dict[str, Any]]] = []
        current_round: List[Dict[str, Any]] = []
        try:
            with open(self.transcript_path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("source") in self.EXCLUDED_RECENT_SOURCES:
                        continue
                    text = entry.get("text", "")
                    if any(snippet in text for snippet in self.EXCLUDED_TEXT_SNIPPETS):
                        continue
                    if entry.get("role") == "user" and current_round:
                        rounds.append(current_round)
                        current_round = [entry]
                    else:
                        current_round.append(entry)
        except Exception as e:
            logger.error(f"Failed to load transcript rounds: {e}")
            return []

        if current_round:
            rounds.append(current_round)

        completed: List[List[Dict[str, Any]]] = []
        for entries in rounds:
            user_indexes = [
                index for index, entry in enumerate(entries) if entry.get("role") == "user"
            ]
            if not user_indexes:
                continue
            user_index = user_indexes[-1]
            assistant_entries = [
                entry
                for entry in entries[user_index + 1 :]
                if entry.get("role") == "assistant" and str(entry.get("text") or "").strip()
            ]
            if not assistant_entries:
                continue
            completed.append([entries[user_index], assistant_entries[-1]])
        return completed

    @staticmethod
    def _word_count(text: str) -> int:
        return len((text or "").split())

    def _read_optional_text(self, path: Path) -> str:
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8").strip()
        except Exception as e:
            logger.error(f"Failed to read handoff context from {path}: {e}")
            return ""

    def _bounded_recent_rounds(
        self,
        *,
        max_rounds: int,
        max_words: int,
    ) -> tuple[list[tuple[int, List[Dict[str, Any]]]], int]:
        rounds = self._load_rounds()
        selected_rounds = rounds[-max_rounds:] if max_rounds > 0 else rounds
        first_sequence = len(rounds) - len(selected_rounds) + 1
        selected = list(
            zip(
                range(first_sequence, first_sequence + len(selected_rounds)),
                selected_rounds,
            )
        )
        total_words = 0
        kept: list[tuple[int, List[Dict[str, Any]]]] = []

        for sequence, round_entries in reversed(selected):
            round_text = "\n".join((entry.get("text") or "") for entry in round_entries).strip()
            round_words = self._word_count(round_text)
            if total_words + round_words > max_words:
                break
            kept.append((sequence, round_entries))
            total_words += round_words

        kept.reverse()
        omitted_count = len(selected) - len(kept)
        self.last_omission_audit = {
            "requested_exchanges": len(selected),
            "included_exchanges": len(kept),
            "omitted_oldest_exchanges": omitted_count,
            "max_words": max_words,
            "reason": "handoff_word_cap" if omitted_count else "",
        }
        if omitted_count:
            logger.warning(
                "Handoff omitted %s oldest complete exchange(s) at max_words=%s",
                omitted_count,
                max_words,
            )
            if self.canonical_audit is not None:
                self.canonical_audit.record(
                    "history_omission",
                    dict(self.last_omission_audit),
                    provenance={
                        "source": "handoff_builder",
                        "transcript": self.transcript_path.name,
                    },
                )
        return kept, total_words

    @staticmethod
    def _render_recent_context(
        kept: list[tuple[int, List[Dict[str, Any]]]],
    ) -> str:
        if not kept:
            return ""

        lines = [
            "--- RECENT CONVERSATION HANDOFF ---",
        ]

        for sequence, round_entries in kept:
            user_ts = str(round_entries[0].get("ts") or "unknown-time")
            assistant_ts = str(round_entries[-1].get("ts") or "unknown-time")
            lines.append(
                f"Exchange sequence={sequence}; user_ts={user_ts}; assistant_ts={assistant_ts}:"
            )
            for entry in round_entries:
                role = str(entry.get("role", "unknown")).upper()
                text = (entry.get("text") or "").strip()
                if text:
                    lines.append(f"{role}: {text}")
            lines.append("")

        return "\n".join(lines).strip()

    def build_recent_context_block(self, max_rounds: int = 10, max_words: int = 6000) -> tuple[str, int, int]:
        kept, total_words = self._bounded_recent_rounds(
            max_rounds=max_rounds,
            max_words=max_words,
        )
        return self._render_recent_context(kept), len(kept), total_words

    def get_recent_rounds(self, max_rounds: int = 10) -> List[List[Dict[str, Any]]]:
        rounds = self._load_rounds()
        return rounds[-max_rounds:] if max_rounds > 0 else rounds

    def build_transfer_package(
        self,
        *,
        transfer_id: str,
        source_agent: str,
        source_instance: str,
        target_agent: str,
        target_instance: str,
        created_at: str,
        max_rounds: int = 10,
        max_words: int = 6000,
    ) -> dict[str, Any]:
        bounded_rounds, word_count = self._bounded_recent_rounds(
            max_rounds=max_rounds,
            max_words=max_words,
        )
        context_block = self._render_recent_context(bounded_rounds)
        exchange_count = len(bounded_rounds)
        recent_rounds = [round_entries for _sequence, round_entries in bounded_rounds]
        last_user_text = ""
        last_assistant_text = ""
        rendered_rounds: list[dict[str, Any]] = []

        for round_index, round_entries in enumerate(recent_rounds, start=1):
            rendered_entries = []
            for entry in round_entries:
                role = str(entry.get("role") or "")
                text = (entry.get("text") or "").strip()
                source = str(entry.get("source") or "")
                if not text:
                    continue
                rendered_entries.append(
                    {
                        "role": role,
                        "source": source,
                        "text": text,
                    }
                )
                if role == "user":
                    last_user_text = text
                elif role == "assistant":
                    last_assistant_text = text
            if rendered_entries:
                rendered_rounds.append({"index": round_index, "entries": rendered_entries})

        handoff_summary = self._read_optional_text(self.handoff_path)
        if not handoff_summary:
            self.build_handoff()
            handoff_summary = self._read_optional_text(self.handoff_path)

        memory_files = {}
        for name in ("project.md", "decisions.md", "tasks.md"):
            text = self._read_optional_text(self.memory_dir / name)
            if text:
                memory_files[name] = text

        task_state = {
            "latest_user_request": last_user_text,
            "latest_source_reply": last_assistant_text,
            "recent_exchange_count": exchange_count,
            "handoff_available": bool(handoff_summary),
            "memory_files_available": sorted(memory_files.keys()),
        }

        return {
            "transfer_id": transfer_id,
            "source_agent": source_agent,
            "source_instance": source_instance,
            "target_agent": target_agent,
            "target_instance": target_instance,
            "created_at": created_at,
            "exchange_count": exchange_count,
            "word_count": word_count,
            "omission_audit": dict(self.last_omission_audit),
            "recent_context_block": context_block,
            "recent_rounds": rendered_rounds,
            "last_user_message": last_user_text,
            "last_assistant_message": last_assistant_text,
            "transfer_guidance": {
                "prioritize_recent_turns": True,
                "recent_turn_weighting": "Prefer the newest exchanges for current intent, task state, and next actions.",
                "older_turn_weighting": "Treat older exchanges as background only; they may contain stale topics or superseded assumptions.",
                "conflict_rule": "If older context conflicts with newer instructions or task state, follow the newer context.",
            },
            "task_state": task_state,
            "handoff_summary": handoff_summary,
            "memory_files": memory_files,
        }

    def build_session_restore_prompt(self, max_rounds: int = 10, max_words: int = 6000) -> tuple[str, int, int]:
        context_block, exchange_count, total_words = self.build_recent_context_block(
            max_rounds=max_rounds,
            max_words=max_words,
        )
        lines = [
            "SYSTEM: Start a fresh session, but preserve continuity from the recent bridge-managed transcript below.",
            "Use it as background memory for unresolved work, user preferences, decisions, and recent activity.",
            "Do not repeat the whole transcript back. Give a short acknowledgement that you have restored context and are ready to continue.",
            "",
        ]
        if context_block:
            lines.append(context_block)
            lines.append("")

        lines.extend(
            [
                "--- CURRENT USER REQUEST — AUTHORITATIVE ---",
                "Acknowledge that you have restored recent context from bridge history and are ready for the next instruction.",
            ]
        )
        return "\n".join(lines).strip(), exchange_count, total_words

    def refresh_recent_context(self):
        """Reads transcript, extracts last N rounds, writes to recent_context."""
        rounds = self._load_rounds()
        if not rounds:
            return

        try:
            recent_rounds = rounds[-self.max_recent_rounds:]
            
            with open(self.recent_context_path, "w", encoding="utf-8") as f:
                for r in recent_rounds:
                    for entry in r:
                        f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.error(f"Failed to refresh recent context: {e}")

    def build_handoff(self):
        """Synthesizes handoff.md from memory files and recent context."""
        handoff_content = ["# Handoff Summary\n\n"]
        
        # Read key memory files if they exist
        key_files = ["project.md", "decisions.md", "tasks.md"]
        for kf in key_files:
            file_path = self.memory_dir / kf
            if file_path.exists():
                handoff_content.append(f"## {kf.replace('.md', '').capitalize()}\n")
                handoff_content.append(file_path.read_text(encoding="utf-8").strip() + "\n\n")

        # Read recent context summary
        handoff_content.append("## Recent Context Summary\n")
        if self.recent_context_path.exists():
            try:
                with open(self.recent_context_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                # Just adding a simple summary or latest few interactions
                # A proper summary might need an LLM call, but for V1 we just dump last few messages
                handoff_content.append("Last few exchanges:\n")
                for line in lines[-10:]: # last 10 messages max in handoff explicitly
                    entry = json.loads(line)
                    role = entry.get("role", "unknown")
                    text = entry.get("text", "").replace("\n", " ")[:200] + ("..." if len(entry.get("text", "")) > 200 else "")
                    handoff_content.append(f"**{role.capitalize()}**: {text}\n")
            except Exception as e:
                logger.error(f"Failed to parse recent context for handoff: {e}")
        else:
            handoff_content.append("No recent context available.\n")

        try:
            self.handoff_path.write_text("".join(handoff_content), encoding="utf-8")
            logger.info("Successfully rebuilt handoff.md")
        except Exception as e:
            logger.error(f"Failed to write handoff.md: {e}")
