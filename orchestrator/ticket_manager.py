"""
HASHI IT Support Ticket Manager
================================
Program-driven ticketing system that works WITHOUT LLM or backend.
Agents use /ticket to report issues; Arale receives and triages them.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("TicketManager")

# ---------------------------------------------------------------------------
# Ticket ID generation
# ---------------------------------------------------------------------------

def _next_ticket_id(tickets_dir: Path) -> str:
    """Generate TKT-YYYYMMDD-NNN, scanning existing files to avoid collision."""
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"TKT-{today}-"
    existing = set()
    for subdir in ("open", "in_progress", "resolved"):
        d = tickets_dir / subdir
        if d.is_dir():
            for f in d.iterdir():
                if f.stem.startswith(prefix):
                    try:
                        existing.add(int(f.stem.split("-")[-1]))
                    except ValueError:
                        pass
    seq = 1
    while seq in existing:
        seq += 1
    return f"{prefix}{seq:03d}"


# ---------------------------------------------------------------------------
# Diagnostic collector (pure Python — no LLM needed)
# ---------------------------------------------------------------------------

def _read_tail(path: Path, lines: int = 50) -> str:
    """Read last N lines of a file, safely."""
    try:
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])
    except Exception as e:
        return f"[read error: {e}]"


def _git_status_short(repo_dir: Path) -> str:
    """Run git status --short, cross-platform safe."""
    try:
        r = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=str(repo_dir),
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip()[:500] if r.returncode == 0 else f"[git error: {r.stderr.strip()[:200]}]"
    except Exception as e:
        return f"[git unavailable: {e}]"


def _disk_usage(path: Path) -> dict:
    try:
        usage = shutil.disk_usage(str(path))
        return {
            "total_gb": round(usage.total / (1 << 30), 1),
            "free_gb": round(usage.free / (1 << 30), 1),
            "used_pct": round((usage.used / usage.total) * 100, 1),
        }
    except Exception:
        return {}


def _recent_transcript(transcript_path: Path, max_rounds: int = 3) -> list[dict]:
    """Read last N entries from transcript.jsonl."""
    try:
        if not transcript_path.exists():
            return []
        lines = transcript_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        entries = []
        for line in lines[-max_rounds * 2:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return entries[-max_rounds * 2:]
    except Exception:
        return []


def collect_diagnostics(
    *,
    agent_name: str,
    workspace_dir: Path,
    project_root: Path,
) -> dict[str, Any]:
    """Collect diagnostic info without any LLM involvement."""
    diag: dict[str, Any] = {}

    # Error log (look for errors.log in session dirs or workspace)
    errors_log = workspace_dir / "errors.log"
    if not errors_log.exists():
        # Try latest session dir
        logs_dir = project_root / "logs" / agent_name
        if logs_dir.is_dir():
            sessions = sorted(logs_dir.iterdir(), key=lambda p: p.name, reverse=True)
            for s in sessions[:1]:
                candidate = s / "errors.log"
                if candidate.exists():
                    errors_log = candidate
                    break

    diag["last_errors"] = _read_tail(errors_log, lines=30)
    diag["error_log_path"] = str(errors_log) if errors_log.exists() else None

    # Recent transcript
    transcript_path = workspace_dir / "transcript.jsonl"
    recent = _recent_transcript(transcript_path, max_rounds=3)
    diag["recent_context"] = recent

    # Git status
    diag["git_status"] = _git_status_short(project_root)

    # Disk
    diag["disk"] = _disk_usage(project_root)

    # Platform info
    diag["platform"] = platform.system()
    diag["python"] = platform.python_version()

    # Backend state
    state_path = workspace_dir / "state.json"
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            diag["backend_status"] = state.get("backend", "unknown")
            diag["active_task"] = state.get("current_task", None)
        except Exception:
            diag["backend_status"] = "unknown"
    else:
        diag["backend_status"] = "unknown"

    return diag


# ---------------------------------------------------------------------------
# Ticket CRUD
# ---------------------------------------------------------------------------

def _resolve_tickets_dir(project_root: Path) -> Path:
    """Resolve tickets dir, handling WSL ↔ Windows paths."""
    tickets = project_root / "tickets"
    tickets.mkdir(exist_ok=True)
    for sub in ("open", "in_progress", "resolved"):
        (tickets / sub).mkdir(exist_ok=True)
    return tickets


def create_ticket(
    *,
    project_root: Path,
    source_agent: str,
    source_instance: str,
    workspace_dir: Path,
    summary: str,
    priority: str = "auto",
) -> dict[str, Any]:
    """
    Create a ticket JSON file in tickets/open/.
    This is the CORE function called by cmd_ticket — pure Python, no LLM.
    """
    tickets_dir = _resolve_tickets_dir(project_root)
    ticket_id = _next_ticket_id(tickets_dir)

    diag = collect_diagnostics(
        agent_name=source_agent,
        workspace_dir=workspace_dir,
        project_root=project_root,
    )

    ticket = {
        "ticket_id": ticket_id,
        "created_at": datetime.now().astimezone().isoformat(),
        "source_agent": source_agent,
        "source_instance": source_instance,
        "status": "open",
        "priority": priority,
        "summary": summary,
        "auto_collected": diag,
        "resolution": None,
    }

    ticket_path = tickets_dir / "open" / f"{ticket_id}.json"
    ticket_path.write_text(json.dumps(ticket, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    logger.info(f"Ticket created: {ticket_id} by {source_agent}@{source_instance}")
    return ticket


def load_ticket(ticket_path: Path) -> dict[str, Any]:
    return json.loads(ticket_path.read_text(encoding="utf-8"))


def move_ticket(ticket_path: Path, new_status: str, tickets_dir: Path) -> Path:
    """Move ticket file between open/in_progress/resolved."""
    dest_dir = tickets_dir / new_status
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / ticket_path.name
    ticket = load_ticket(ticket_path)
    ticket["status"] = new_status
    if new_status == "resolved":
        ticket["resolved_at"] = datetime.now().astimezone().isoformat()
    dest.write_text(json.dumps(ticket, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    ticket_path.unlink(missing_ok=True)
    return dest


def update_ticket_resolution(ticket_path: Path, resolution: dict) -> None:
    """Write resolution details to an existing ticket."""
    ticket = load_ticket(ticket_path)
    ticket["resolution"] = resolution
    ticket_path.write_text(json.dumps(ticket, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def list_tickets(tickets_dir: Path, status: str = "open") -> list[dict]:
    """List tickets in a given status folder."""
    d = tickets_dir / status
    if not d.is_dir():
        return []
    tickets = []
    for f in sorted(d.glob("TKT-*.json")):
        try:
            tickets.append(load_ticket(f))
        except Exception:
            pass
    return tickets


# ---------------------------------------------------------------------------
# Instance detection
# ---------------------------------------------------------------------------

def detect_instance(project_root: Path) -> str:
    """Detect which HASHI instance we're running on."""
    root_str = str(project_root).replace("\\", "/").lower()
    if "hashi2" in root_str:
        return "HASHI2"
    if "/mnt/c/" in root_str or "c:" in root_str.lower():
        return "HASHI9"
    return "HASHI1"


# ---------------------------------------------------------------------------
# Notification helpers (called from cmd_ticket handler)
# ---------------------------------------------------------------------------

def format_ticket_notification(ticket: dict) -> str:
    """Format a ticket for Telegram notification."""
    lines = [
        f"🎫 New Ticket: {ticket['ticket_id']}",
        f"From: {ticket['source_agent']}@{ticket['source_instance']}",
        f"Priority: {ticket['priority']}",
        f"Summary: {ticket['summary']}",
        "",
        f"Status: {ticket['status']}",
    ]
    diag = ticket.get("auto_collected", {})
    if diag.get("last_errors"):
        err_preview = diag["last_errors"][:200]
        lines.append(f"\nRecent errors:\n{err_preview}")
    if diag.get("git_status"):
        lines.append(f"\nGit: {diag['git_status'][:150]}")
    return "\n".join(lines)


def format_ticket_list(tickets: list[dict]) -> str:
    """Format a list of tickets for display."""
    if not tickets:
        return "No tickets found."
    lines = []
    for t in tickets:
        lines.append(f"[{t['ticket_id']}] {t['source_agent']}@{t['source_instance']} — {t['summary'][:60]}")
    return "\n".join(lines)
