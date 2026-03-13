from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import List, Dict, Any

logger = logging.getLogger("BridgeU.HandoffBuilder")

class HandoffBuilder:
    EXCLUDED_RECENT_SOURCES = {"startup", "system", "think"}
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
        "Understood. I’ll treat `HANDOFF SUMMARY`",
        "Understood. I’ll use that material only as background",
        "Session instructions in effect:",
        "**Handoff Summary**",
    )

    def __init__(self, workspace_dir: Path, transcript_filename: str = "transcript.jsonl"):
        self.workspace_dir = workspace_dir
        self.transcript_path = workspace_dir / transcript_filename
        self.recent_context_path = workspace_dir / "recent_context.jsonl"
        self.handoff_path = workspace_dir / "handoff.md"
        self.memory_dir = workspace_dir / "memory"
        
        self.max_recent_rounds = 15

    def append_transcript(self, role: str, text: str, source: str = "text"):
        entry = {
            "role": role,
            "text": text,
            "source": source
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
        return rounds

    @staticmethod
    def _word_count(text: str) -> int:
        return len((text or "").split())

    def build_recent_context_block(self, max_rounds: int = 10, max_words: int = 6000) -> tuple[str, int, int]:
        rounds = self._load_rounds()
        selected = rounds[-max_rounds:] if max_rounds > 0 else rounds
        total_words = 0
        kept: List[List[Dict[str, Any]]] = []

        for round_entries in reversed(selected):
            round_text = "\n".join((entry.get("text") or "") for entry in round_entries).strip()
            round_words = self._word_count(round_text)
            if kept and total_words + round_words > max_words:
                break
            if not kept and round_words > max_words:
                clipped_words = " ".join(round_text.split()[:max_words]).strip()
                kept.append([{"role": "system", "text": clipped_words, "source": "handoff-clipped"}])
                total_words = self._word_count(clipped_words)
                break
            kept.append(round_entries)
            total_words += round_words

        kept.reverse()
        if not kept:
            return "", 0, 0

        lines = [
            "--- RECENT CONVERSATION HANDOFF ---",
        ]

        exchange_count = 0
        for index, round_entries in enumerate(kept, start=1):
            exchange_count += 1
            lines.append(f"Exchange {index}:")
            for entry in round_entries:
                role = str(entry.get("role", "unknown")).upper()
                text = (entry.get("text") or "").strip()
                if text:
                    lines.append(f"{role}: {text}")
            lines.append("")

        return "\n".join(lines).strip(), exchange_count, total_words

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
                "--- NEW REQUEST ---",
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
