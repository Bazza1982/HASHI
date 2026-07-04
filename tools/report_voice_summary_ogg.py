#!/usr/bin/env python3
"""Generate an OGG voice asset from a report's voice-summary section.

This is intentionally a sidecar utility: it does not alter, summarize, or send
the original text report. Scheduled report prompts can append a short
"语音摘要稿" section, then call this script to synthesize only that section.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestrator.tts_providers.edge import EdgeTTSProvider  # noqa: E402


DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
DEFAULT_OUTPUT_DIR = ROOT / "media" / "sunny" / "report_voice_summaries"


VOICE_SECTION_PATTERNS = [
    re.compile(r"^\s*(?:#{1,6}\s*)?(?:🎙️\s*)?(?:\*\*)?语音摘要稿(?:\*\*)?\s*[:：]?\s*$"),
    re.compile(r"^\s*(?:#{1,6}\s*)?(?:🎙️\s*)?(?:\*\*)?语音摘要(?:\*\*)?\s*[:：]?\s*$"),
    re.compile(r"^\s*(?:#{1,6}\s*)?(?:🎙️\s*)?(?:\*\*)?Voice Summary(?:\*\*)?\s*[:：]?\s*$", re.I),
]


def _looks_like_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith("#"):
        return True
    # Common report section headings: **📬 Gmail...**, **📚 ...**
    if stripped.startswith("**") and stripped.endswith("**"):
        return True
    # Emoji-led top-level headings used by scheduled reports.
    return bool(re.match(r"^[🔴🟠📎⚠️✅📊📚📬📌🔔📄]\s+", stripped))


def extract_voice_summary(report_text: str) -> str:
    """Return the voice-summary section from a report, or an empty string."""
    lines = report_text.splitlines()
    start_idx: int | None = None
    for idx, line in enumerate(lines):
        if any(pattern.match(line) for pattern in VOICE_SECTION_PATTERNS):
            start_idx = idx + 1
            break

    if start_idx is None:
        return ""

    collected: list[str] = []
    for line in lines[start_idx:]:
        if collected and _looks_like_heading(line):
            break
        collected.append(line)

    text = "\n".join(collected).strip()
    # Remove a single surrounding fenced block if someone pasted the script as code.
    text = re.sub(r"^```(?:text|markdown)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def read_input(args: argparse.Namespace) -> str:
    if args.text:
        return args.text
    if args.input:
        return Path(args.input).read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("Provide --text, --input, or stdin.")


def build_stem(prefix: str) -> str:
    safe_prefix = re.sub(r"[^A-Za-z0-9_.-]+", "_", prefix.strip() or "report_voice_summary")
    return f"{safe_prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


async def synthesize_ogg(text: str, args: argparse.Namespace) -> Path:
    output_dir = Path(args.output_dir).expanduser().resolve()
    provider = EdgeTTSProvider(ffmpeg_cmd=args.ffmpeg)
    asset = await provider.synthesize(
        text=text,
        output_dir=output_dir,
        stem=build_stem(args.prefix),
        voice_name=args.voice,
        rate=args.rate,
        max_chars=args.max_chars,
        provider_options={"rate_percent": args.rate_percent},
    )
    return asset.ogg_path


def send_telegram(ogg_path: Path, args: argparse.Namespace) -> None:
    cmd = [
        sys.executable,
        str(ROOT / "tools" / "telegram_send_file_cli.py"),
        "--path",
        str(ogg_path),
        "--type",
        args.telegram_type,
    ]
    if args.telegram_caption:
        cmd.extend(["--caption", args.telegram_caption])
    if args.telegram_chat_id:
        cmd.extend(["--chat-id", args.telegram_chat_id])

    env = dict(os.environ)
    if args.telegram_agent:
        env.setdefault("HASHI_AGENT_NAME", args.telegram_agent)

    result = subprocess.run(cmd, cwd=str(ROOT), env=env, text=True, capture_output=True)
    if result.stdout.strip():
        print(result.stdout.strip(), file=sys.stderr)
    if result.returncode != 0:
        if result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
        raise RuntimeError(f"Telegram send failed with exit code {result.returncode}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate an OGG file from a report's 语音摘要稿 section.",
    )
    parser.add_argument("--input", help="Markdown/text report path. Defaults to stdin.")
    parser.add_argument("--text", help="Direct voice-summary text or full report text.")
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Treat input as the exact text to speak instead of extracting 语音摘要稿.",
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for generated audio.")
    parser.add_argument("--prefix", default="sunny_report_voice", help="Output filename prefix.")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help="Edge TTS voice name.")
    parser.add_argument("--rate", type=int, default=0, help="Compatibility rate knob; rate-percent wins.")
    parser.add_argument("--rate-percent", type=int, default=0, help="Edge TTS speaking rate percent.")
    parser.add_argument("--max-chars", type=int, default=900, help="Maximum spoken characters.")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg command path.")
    parser.add_argument("--send-telegram", action="store_true", help="Send generated OGG to Telegram.")
    parser.add_argument("--telegram-chat-id", default=None, help="Override Telegram chat ID.")
    parser.add_argument("--telegram-agent", default="sunny", help="HASHI agent name for Telegram token lookup.")
    parser.add_argument(
        "--telegram-type",
        default="voice",
        choices=["voice", "audio"],
        help="Telegram send mode. voice uses sendVoice; audio uses sendAudio.",
    )
    parser.add_argument("--telegram-caption", default="语音摘要", help="Telegram caption for audio mode.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_text = read_input(args)
    voice_text = source_text.strip() if args.summary_only else extract_voice_summary(source_text)
    if not voice_text:
        print("No voice summary section found; no OGG generated.", file=sys.stderr)
        return 2

    ogg_path = asyncio.run(synthesize_ogg(voice_text, args))
    print(str(ogg_path))
    if args.send_telegram:
        send_telegram(ogg_path, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
