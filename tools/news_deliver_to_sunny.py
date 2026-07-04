#!/usr/bin/env python3
"""Relay Hermes xiaoye news cron output to the HASHI sunny Telegram bot."""

from __future__ import annotations

import argparse
import io
import json
import mimetypes
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import request as urllib_request
from zoneinfo import ZoneInfo

SYDNEY = ZoneInfo("Australia/Sydney")
CHAT_ID = "7430217666"
AGENT = "sunny"
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
    for candidate in (
        Path("/mnt/c/Users/thene/AppData/Local/hermes/profiles/xiaoye"),
        Path(r"C:/Users/thene/AppData/Local/hermes/profiles/xiaoye"),
    ):
        if (candidate / "cron" / "jobs.json").exists():
            return candidate
    raise SystemExit("Hermes xiaoye profile not found")


def load_secrets() -> dict:
    for candidate in (
        HASHI_ROOT / "secrets.json",
        Path("/home/lily/projects/hashi/secrets.json"),
    ):
        if candidate.exists():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise SystemExit("HASHI secrets.json not found")


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


def telegram_post(token: str, method: str, fields: dict, file_field: tuple[str, Path] | None = None) -> dict:
    boundary = "----HASHINewsRelayBoundary"
    body = io.BytesIO()

    def write(text: str) -> None:
        body.write(text.encode("utf-8"))

    for key, value in fields.items():
        write(f"--{boundary}\r\n")
        write(f'Content-Disposition: form-data; name="{key}"\r\n\r\n')
        write(f"{value}\r\n")

    if file_field:
        field_name, file_path = file_field
        mime_type, _ = mimetypes.guess_type(str(file_path))
        mime_type = mime_type or "application/octet-stream"
        write(f"--{boundary}\r\n")
        write(
            f'Content-Disposition: form-data; name="{field_name}"; filename="{file_path.name}"\r\n'
        )
        write(f"Content-Type: {mime_type}\r\n\r\n")
        body.write(file_path.read_bytes())
        write("\r\n")

    write(f"--{boundary}--\r\n")
    url = f"https://api.telegram.org/bot{token}/{method}"
    req = urllib_request.Request(
        url,
        data=body.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib_request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send_text(token: str, text: str) -> None:
    if not text:
        return
    chunk_size = 4000
    chunks = [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]
    for idx, chunk in enumerate(chunks, start=1):
        result = telegram_post(token, "sendMessage", {"chat_id": CHAT_ID, "text": chunk})
        if not result.get("ok"):
            raise RuntimeError(result.get("description", "sendMessage failed"))


def send_voice(token: str, path: Path) -> None:
    result = telegram_post(token, "sendVoice", {"chat_id": CHAT_ID}, ("voice", path))
    if not result.get("ok"):
        raise RuntimeError(result.get("description", f"sendVoice failed for {path.name}"))


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

    secrets = load_secrets()
    token = secrets.get(AGENT)
    if not token:
        raise SystemExit(f"No Telegram token for agent '{AGENT}' in secrets.json")

    send_text(str(token), text)
    for voice_path in media_paths:
        send_voice(str(token), voice_path)

    state[job_id] = {
        "delivered_file": output_file.name,
        "delivered_at": datetime.now(SYDNEY).isoformat(),
        "delivered_date": sydney_today(),
        "voice_files": [str(p) for p in media_paths],
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
