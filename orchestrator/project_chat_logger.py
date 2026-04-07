"""project_chat_logger.py — Log agent chat exchanges to per-project files.

When a chat message carries a MINATO CONTEXT header (cross-instance hchat from Minato),
this module parses the project/shimanto/nagare metadata and writes an entry to:
    workspaces/<agent>/projects/<project_slug>/chat_log.jsonl

Each entry records the full exchange (user + assistant) with metadata so the project
accumulates a persistent, tagged chat history across reboots.
"""

import json
import re
from datetime import datetime
from pathlib import Path


_CONTEXT_RE = re.compile(
    r"\[MINATO CONTEXT[^\]]*\](.*?)\[END CONTEXT\]", re.DOTALL
)


def parse_minato_context(text: str) -> dict | None:
    """Extract project/shimanto/nagare info from a MINATO CONTEXT block in message text."""
    m = _CONTEXT_RE.search(text)
    if not m:
        return None
    body = m.group(1)
    ctx: dict = {}
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("minato active project:"):
            ctx["active_project"] = line.split(":", 1)[1].strip()
        elif line.startswith("shimanto phases:"):
            phases_raw = line.split(":", 1)[1].strip()
            ctx["shimanto_phases"] = [p.strip() for p in phases_raw.split(",") if p.strip()]
        elif line.startswith("nagare workflows:"):
            wf_raw = line.split(":", 1)[1].strip()
            # "0 workflow(s)" → [] ; "wf1, wf2" or "wf1, wf2 (2 workflow(s))" → ["wf1", "wf2"]
            wf_raw = re.sub(r"\s*\d+ workflow\(s\)", "", wf_raw).strip().rstrip(",").strip()
            ctx["nagare_workflows"] = [w.strip() for w in wf_raw.split(",") if w.strip() and w.strip() != "0"]
        elif line.startswith("scope:"):
            ctx["scope"] = line.split(":", 1)[1].strip()
    return ctx if ctx else None


class ProjectChatLogger:
    """Writes per-project chat log entries to workspaces/<agent>/projects/<slug>/chat_log.jsonl."""

    def __init__(self, workspace_dir: Path):
        self.workspace_dir = workspace_dir

    def _project_slug(self, project_name: str) -> str:
        slug = project_name.lower()
        slug = re.sub(r"['\"]", "", slug)
        slug = re.sub(r"[^a-z0-9]+", "_", slug)
        return slug.strip("_") or "default"

    def log_exchange(
        self,
        user_msg: str,
        assistant_msg: str,
        source: str,
        active_project: str | None = None,
        shimanto_phases: list | None = None,
        nagare_workflows: list | None = None,
        scope: str | None = None,
    ) -> None:
        """Append one exchange to the active project's chat log.

        If active_project is None, tries to parse a MINATO CONTEXT block from user_msg.
        Does nothing if no project can be determined.
        """
        if not active_project:
            ctx = parse_minato_context(user_msg)
            if ctx:
                active_project = ctx.get("active_project")
                shimanto_phases = shimanto_phases or ctx.get("shimanto_phases", [])
                nagare_workflows = nagare_workflows or ctx.get("nagare_workflows", [])
                scope = scope or ctx.get("scope")

        if not active_project:
            return

        slug = self._project_slug(active_project)
        project_dir = self.workspace_dir / "projects" / slug
        project_dir.mkdir(parents=True, exist_ok=True)

        entry = {
            "ts": datetime.now().isoformat(),
            "source": source,
            "project": active_project,
            "shimanto_phases": shimanto_phases or [],
            "nagare_workflows": nagare_workflows or [],
            "scope": scope or "",
            "user": user_msg,
            "assistant": assistant_msg,
        }
        chat_log_path = project_dir / "chat_log.jsonl"
        with open(chat_log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
