#!/usr/bin/env python3
"""Relay Hermes xiaoye news cron output to the HASHI sunny Telegram bot."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

SYDNEY = ZoneInfo("Australia/Sydney")
AGENT = os.environ.get("HASHI_NEWS_AGENT", "sunny").strip() or "sunny"
HASHI_ROOT = Path(__file__).resolve().parent.parent

NEWS_JOBS: dict[str, dict[str, object]] = {
    "c2fcf85a5c81": {"name": "早报", "earliest_time": "07:00"},
    "fccbe1d2aec2": {"name": "晚报", "earliest_time": "17:00"},
    "3499c28f43c9": {"name": "夜话", "earliest_time": "21:00"},
}

MEDIA_RE = re.compile(r"^MEDIA:(.+)$", re.MULTILINE)
SKIP_PATTERNS = [
    re.compile(r"\[SILENT\]", re.I),
    re.compile(r"生成失败报告"),
]


def resolve_hermes_home() -> Path:
    configured = os.environ.get("HASHI_HERMES_HOME", "").strip()
    if not configured:
        raise SystemExit("Set HASHI_HERMES_HOME to the Hermes profile directory")
    candidate = Path(configured).expanduser()
    if (candidate / "cron" / "jobs.json").exists():
        return candidate.resolve()
    raise SystemExit(f"Hermes profile not found at HASHI_HERMES_HOME={candidate}")


def load_secrets() -> dict:
    configured = os.environ.get("HASHI_SECRETS_PATH", "").strip()
    candidates = [Path(configured).expanduser()] if configured else [HASHI_ROOT / "secrets.json"]
    for candidate in candidates:
        if candidate.exists():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise SystemExit(
        "HASHI secrets.json not found; set HASHI_SECRETS_PATH for an external instance"
    )


def telegram_chat_id() -> str:
    chat_id = os.environ.get("HASHI_NEWS_CHAT_ID", "").strip()
    if not chat_id:
        raise SystemExit("Set HASHI_NEWS_CHAT_ID to the destination Telegram chat")
    return chat_id


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def latest_output_file(home: Path, job_id: str) -> Path | None:
    out_dir = home / "cron" / "output" / job_id
    if not out_dir.exists():
        return None
    files = sorted(out_dir.glob("*.md"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def extract_response(md_text: str) -> str:
    if "## Response" not in md_text:
        return md_text.strip()
    return md_text.rsplit("## Response", 1)[-1].strip()


def clean_text_for_telegram(response: str) -> str:
    lines: list[str] = []
    for line in response.splitlines():
        stripped = line.strip()
        if stripped.startswith("[[audio_as_voice]]"):
            continue
        if stripped.startswith("MEDIA:"):
            continue
        lines.append(line)
    text = "\n".join(lines).strip()
    return text


def normalize_media_path(raw: str) -> Path | None:
    raw = raw.strip().strip("`")
    if not raw or "..." in raw:
        return None

    candidates: list[Path] = [Path(raw)]
    if re.match(r"^[A-Za-z]:[\\/]", raw):
        drive = raw[0].lower()
        rest = raw[2:].replace("\\", "/").lstrip("/")
        candidates.append(Path(f"/mnt/{drive}/{rest}"))
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def parse_media_paths(response: str) -> list[Path]:
    paths: list[Path] = []
    for match in MEDIA_RE.finditer(response):
        resolved = normalize_media_path(match.group(1))
        if resolved is not None:
            paths.append(resolved)
    return paths


def should_skip_response(response: str) -> str | None:
    if not response.strip():
        return "empty_response"
    for pat in SKIP_PATTERNS:
        if pat.search(response):
            return "silent_or_failure"
    return None


def sydney_today() -> str:
    return datetime.now(SYDNEY).date().isoformat()


def output_date_from_name(output_file: Path) -> str | None:
    match = re.match(r"^(\d{4}-\d{2}-\d{2})_", output_file.name)
    return match.group(1) if match else None


def output_datetime_from_name(output_file: Path) -> datetime | None:
    match = re.match(r"^(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})", output_file.name)
    if not match:
        return None
    date_part, hh, mm, ss = match.groups()
    try:
        return datetime.fromisoformat(f"{date_part}T{hh}:{mm}:{ss}").replace(tzinfo=SYDNEY)
    except ValueError:
        return None


def is_fresh_output(job_id: str, output_file: Path) -> bool:
    output_dt = output_datetime_from_name(output_file)
    if output_dt is None or output_dt.date().isoformat() != sydney_today():
        return False
    earliest = str(NEWS_JOBS[job_id].get("earliest_time", "00:00"))
    earliest_hh, earliest_mm = [int(part) for part in earliest.split(":", 1)]
    earliest_dt = output_dt.replace(hour=earliest_hh, minute=earliest_mm, second=0, microsecond=0)
    return output_dt >= earliest_dt


def already_delivered(job_id: str, output_file: Path, state: dict) -> bool:
    entry = state.get(job_id) or {}
    return entry.get("delivered_file") == output_file.name


async def publish_news_via_fc(
    *,
    job_id: str,
    output_file: Path,
    text: str,
    media_paths: list[Path],
    force: bool,
) -> list[dict]:
    """Commit news outputs to PAO state, then let the Telegram Connector send."""

    secrets = load_secrets()
    config_path = HASHI_ROOT / "agents.json"
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    agent = next(
        (
            item
            for item in config.get("agents") or ()
            if isinstance(item, dict)
            and str(item.get("name") or "").strip().casefold()
            == AGENT.casefold()
        ),
        None,
    )
    if agent is None:
        raise RuntimeError(f"Agent is not configured: {AGENT}")
    token_key = str(agent.get("telegram_token_key") or AGENT)
    token = str(secrets.get(token_key) or secrets.get(AGENT) or "").strip()
    if not token or token == "WORKBENCH_ONLY_NO_TOKEN":
        raise RuntimeError(f"No Telegram Connector token for agent '{AGENT}'")
    configured_owner = (
        os.environ.get("HASHI_NEWS_OWNER_ID")
        or os.environ.get("HASHI_OWNER_ID")
    )
    configured_authorized = (
        os.environ.get("HASHI_AUTHORIZED_TELEGRAM_ID")
        or secrets.get("_authorized_telegram_id")
    )
    if configured_authorized is None:
        raise RuntimeError("No authorized owner identity for news FC publication")
    authorized_id = int(configured_authorized)
    owner_id = str(configured_owner or f"user:{authorized_id}")
    chat_id = int(telegram_chat_id())
    force_suffix = f":force:{uuid4().hex}" if force else ""
    base_material = f"{job_id}:{output_file.name}{force_suffix}"
    from orchestrator.frontend_telegram_connector import (
        publish_explicit_telegram_notification,
    )

    common = {
        "root": HASHI_ROOT,
        "instance_id": str(
            os.environ.get("HASHI_INSTANCE_ID")
            or (config.get("global") or {}).get("instance_id")
            or "HASHI"
        ),
        "agent_id": AGENT,
        "owner_id": owner_id,
        "authorized_id": authorized_id,
        "chat_id": chat_id,
        "token": token,
        "session_db_path": os.environ.get("HASHI_SESSION_STORE_DB") or None,
        "attachment_root": os.environ.get("HASHI_SESSION_ATTACHMENT_ROOT") or None,
        "agent_lifecycle_id": str(agent.get("agent_lifecycle_id") or ""),
    }
    results = [
        await publish_explicit_telegram_notification(
            **common,
            publication_id="news-text-"
            + hashlib.sha256(base_material.encode("utf-8")).hexdigest(),
            text=text,
        )
    ]
    for index, voice_path in enumerate(media_paths):
        results.append(
            await publish_explicit_telegram_notification(
                **common,
                publication_id="news-media-"
                + hashlib.sha256(
                    f"{base_material}:{index}:{voice_path.name}".encode("utf-8")
                ).hexdigest(),
                file_path=voice_path,
                semantic_role="voice_message",
            )
        )
    if not all(result.get("accepted") for result in results):
        raise RuntimeError(
            "FC delivery was not accepted: "
            + ", ".join(str(result.get("state")) for result in results)
        )
    return results


def relay_job(job_id: str, *, force: bool = False, dry_run: bool = False) -> int:
    if job_id not in NEWS_JOBS:
        print(f"UNKNOWN_JOB {job_id}", file=sys.stderr)
        return 2

    spec = NEWS_JOBS[job_id]
    home = resolve_hermes_home()
    state_path = home / "cron" / "relay_state.json"
    state = load_state(state_path)

    output_file = latest_output_file(home, job_id)
    if output_file is None:
        print(f"NO_OUTPUT job={job_id}")
        return 0

    if not is_fresh_output(job_id, output_file):
        print(
            f"NO_FRESH_OUTPUT job={job_id} latest_file={output_file.name} "
            f"latest_datetime={output_datetime_from_name(output_file)} "
            f"today={sydney_today()} earliest_time={spec.get('earliest_time')}",
            file=sys.stderr,
        )
        return 1

    if not force and already_delivered(job_id, output_file, state):
        print(f"ALREADY_DELIVERED job={job_id} file={output_file.name}")
        return 0

    md_text = output_file.read_text(encoding="utf-8", errors="replace")
    response = extract_response(md_text)
    skip_reason = should_skip_response(response)
    if skip_reason:
        print(f"FAILED job={job_id} reason={skip_reason} file={output_file.name}", file=sys.stderr)
        return 1

    text = clean_text_for_telegram(response)
    media_paths = parse_media_paths(response)
    if not text:
        print(f"SKIP job={job_id} reason=no_text file={output_file.name}", file=sys.stderr)
        return 1

    if dry_run:
        print(
            json.dumps(
                {
                    "job_id": job_id,
                    "name": spec["name"],
                    "file": output_file.name,
                    "text_chars": len(text),
                    "voice_files": [str(p) for p in media_paths],
                },
                ensure_ascii=False,
            )
        )
        return 0

    delivery_results = asyncio.run(
        publish_news_via_fc(
            job_id=job_id,
            output_file=output_file,
            text=text,
            media_paths=media_paths,
            force=force,
        )
    )

    state[job_id] = {
        "delivered_file": output_file.name,
        "delivered_at": datetime.now(SYDNEY).isoformat(),
        "delivered_date": sydney_today(),
        "voice_files": [str(p) for p in media_paths],
        "fc_events": [result["event_id"] for result in delivery_results],
    }
    save_state(state_path, state)
    print(
        f"DELIVERED job={job_id} name={spec['name']} file={output_file.name} "
        f"text_chars={len(text)} voice_count={len(media_paths)}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Relay Hermes news cron output to HASHI sunny bot")
    parser.add_argument("--job-id", required=True, choices=sorted(NEWS_JOBS))
    parser.add_argument("--force", action="store_true", help="Deliver even if state says already sent")
    parser.add_argument("--dry-run", action="store_true", help="Parse only; do not send Telegram")
    parser.add_argument(
        "--mark-delivered",
        action="store_true",
        help="Record latest output as delivered without sending (recovery helper)",
    )
    args = parser.parse_args()

    if args.mark_delivered:
        home = resolve_hermes_home()
        state_path = home / "cron" / "relay_state.json"
        state = load_state(state_path)
        output_file = latest_output_file(home, args.job_id)
        if output_file is None:
            print(f"NO_OUTPUT job={args.job_id}")
            return 1
        state[args.job_id] = {
            "delivered_file": output_file.name,
            "delivered_at": datetime.now(SYDNEY).isoformat(),
            "delivered_date": sydney_today(),
            "marked_without_send": True,
        }
        save_state(state_path, state)
        print(f"MARKED job={args.job_id} file={output_file.name}")
        return 0

    return relay_job(args.job_id, force=args.force, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
